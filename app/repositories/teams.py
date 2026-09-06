from collections.abc import Sequence
from uuid import UUID

import asyncpg

from app.domain.teams import TeamEntity, WorkflowStateCategory, WorkflowStateEntity
from app.domain.tenancy import WorkspaceScope


class TeamRepository:
    """SQL access for `teams` and `workflow_states`.

    The repository receives a connection from the service layer. It never
    acquires connections, never touches the pool, and never owns a
    transaction. `asyncpg.Record` never escapes this class.

    Every statement here is scoped by `workspace_id`, including the ones
    that also name a primary key. An id is not an authorisation: a team id
    that reaches this process from a client is a value, and looking it up
    without the tenant predicate is what turns a guessed id into a read of
    another workspace's data.
    """

    async def find_oldest_id(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
    ) -> UUID | None:
        """The workspace's oldest team, or nothing if it has none.

        This exists because issues must be filed against a team and the
        product has no way to say which one yet. "Oldest" is a placeholder
        rule, not a product decision, and it is written here rather than
        left to the caller for one reason: the alternative was a literal
        team UUID somewhere in `app/`, which is a cross-tenant write waiting
        for the day a second workspace exists.

        Scoped and ordered rather than `LIMIT 1` over the table: without the
        workspace predicate this returns some other tenant's team, and
        without a total order it returns an arbitrary one of this tenant's,
        differently on each call and each replica. `id` breaks a
        created_at tie, which is possible because `teams.created_at`
        defaults to now() and a single statement can insert two teams
        sharing it.
        """
        row = await connection.fetchrow(
            """
            SELECT id
            FROM teams
            WHERE workspace_id = $1
            ORDER BY created_at, id
            LIMIT 1
            """,
            scope.workspace_id,
        )

        if row is None:
            return None

        # Annotated rather than returned inline: asyncpg ships no types, so
        # `row["id"]` is Any and would silently satisfy any return type.
        team_id: UUID = row["id"]

        return team_id

    async def list_by_workspace(
        self,
        connection: asyncpg.Connection,
        workspace_id: UUID,
    ) -> list[TeamEntity]:
        """Every team in one workspace, ordered by key.

        Ordered by key rather than by name or created_at because the key is
        what the product shows beside an issue, and because
        `teams_workspace_key_unique` makes the ordering total -- no tie for
        the sort to break arbitrarily, so two calls return the same order.
        """
        rows = await connection.fetch(
            """
            SELECT
                id,
                workspace_id,
                key,
                name,
                created_at
            FROM teams
            WHERE workspace_id = $1
            ORDER BY key
            """,
            workspace_id,
        )

        return [self._to_team(row) for row in rows]

    async def find_by_key(
        self,
        connection: asyncpg.Connection,
        workspace_id: UUID,
        key: str,
    ) -> TeamEntity | None:
        """Resolve `(workspace, key)` to a team, or nothing.

        The comparison is exact and both values are bound as parameters: no
        upper(), no LIKE, no normalisation of the input. `teams_key_format`
        already confines every stored key to uppercase, so a key differing
        only in case is not a team this database can hold, and folding it
        onto a real team would make team addressing case-insensitive --
        which is what that constraint exists to prevent.

        No LIMIT 1: `teams_workspace_key_unique` makes a second matching row
        impossible, and a limit here would claim doubt about a guarantee the
        schema already gives.
        """
        row = await connection.fetchrow(
            """
            SELECT
                id,
                workspace_id,
                key,
                name,
                created_at
            FROM teams
            WHERE workspace_id = $1 AND key = $2
            """,
            workspace_id,
            key,
        )

        if row is None:
            return None

        return self._to_team(row)

    async def find_default_workflow_state_id(
        self,
        connection: asyncpg.Connection,
        workspace_id: UUID,
        team_id: UUID,
    ) -> UUID | None:
        """The state a newly created issue starts in, or nothing if the team
        has none in this workspace.

        Selected by *category* rather than by name or by position, which is
        the same rule 005 used when it placed every pre-existing issue on a
        board: an issue that is not complete belongs in `unstarted`. A team
        may rename 'Todo' or reorder its board, and neither may change where
        a new issue lands -- so neither the name nor `position = 0` is
        consulted. Position only breaks a tie.

        005 seeds exactly one state per category per team, so the tie-break
        is unreachable today. It is written anyway because nothing stops a
        team from having two `unstarted` states later, and an unordered
        `LIMIT 1` would then pick a different one per call and per replica.

        Scoped by workspace as well as by team: without the predicate a
        team id from another tenant would resolve to that tenant's state,
        and the issue would be created pointing at a board its workspace
        cannot see.
        """
        state_id = await connection.fetchval(
            """
            SELECT id
            FROM workflow_states
            WHERE workspace_id = $1 AND team_id = $2 AND type = 'unstarted'
            ORDER BY position, id
            LIMIT 1
            """,
            workspace_id,
            team_id,
        )

        if state_id is None:
            return None

        # Annotated rather than returned inline: asyncpg ships no types, so
        # fetchval is Any and would silently satisfy any return type.
        resolved: UUID = state_id

        return resolved

    async def list_workflow_states(
        self,
        connection: asyncpg.Connection,
        workspace_id: UUID,
        team_ids: Sequence[UUID],
    ) -> list[WorkflowStateEntity]:
        """The workflow states of several teams, in one round trip.

        Batched over `team_ids` rather than queried per team: the caller is
        rendering a list of teams, and a query per team is the N+1 that
        makes a page's cost depend on how many teams a workspace has.

        `ORDER BY team_id, position, id` is total. `position` alone is not
        -- 005 deliberately leaves it non-unique so that reordering a board
        is a single statement -- so without `id` two states sharing a
        position would come back in whatever order the scan produced, and
        the board would reshuffle itself between reads.
        """
        rows = await connection.fetch(
            """
            SELECT
                id,
                workspace_id,
                team_id,
                name,
                type AS category,
                position,
                color,
                created_at
            FROM workflow_states
            WHERE workspace_id = $1 AND team_id = ANY($2::UUID[])
            ORDER BY team_id, position, id
            """,
            workspace_id,
            list(team_ids),
        )

        return [self._to_workflow_state(row) for row in rows]

    async def allocate_issue_number(
        self,
        connection: asyncpg.Connection,
        workspace_id: UUID,
        team_id: UUID,
    ) -> int | None:
        """Claim the next issue number for a team, or nothing if there is no
        such team in this workspace.

        This is the whole concurrency-critical path, and it is one
        statement on purpose.

        `UPDATE ... SET issue_counter = issue_counter + 1 ... RETURNING`
        takes a row-level exclusive lock on the team as it executes and
        holds it until the caller's transaction ends. A second allocation
        for the same team blocks there; when the first transaction commits,
        PostgreSQL re-reads the row it was waiting on and recomputes
        `issue_counter + 1` from the committed value rather than from the
        snapshot the second transaction opened with. So concurrent callers
        cannot receive the same number, however many of them there are.

        Read-then-write -- `SELECT max(number) + 1`, or a SELECT of the
        counter followed by an UPDATE that sets it -- has no such property:
        a plain SELECT takes no lock, so both callers read the same value
        and compute the same next number. That is the failure this method
        exists to make unrepresentable, and it is why the arithmetic is
        inside the UPDATE rather than in Python.

        `issue_counter` is an ordinary column, so the increment is
        transactional: a caller that allocates 7 and then fails takes the
        increment down with it, and the next caller is handed 7 again. That
        is what a per-team sequence could not do -- nextval() is exempt
        from rollback by design -- and it is why the numbering has no holes
        in it after a failed create.

        The caller MUST already be inside the transaction that will insert
        the issue. Allocating in a transaction of its own would commit the
        increment immediately and hand back a number that a later failure
        could not return, and -- worse -- would release the row lock before
        the insert, so nothing would connect the number to the row that
        uses it.

        `workspace_id` is in the predicate as well as `id`. Without it a
        team id from another tenant increments that tenant's counter and
        returns one of its numbers, which is a cross-tenant write performed
        by a lookup that never looked anything up.

        Returns None rather than raising when nothing matched: whether that
        means "no such team" or "not this workspace's team" is a question
        for the service, and the repository does not answer questions about
        what an absence means.
        """
        number = await connection.fetchval(
            """
            UPDATE teams
            SET issue_counter = issue_counter + 1
            WHERE workspace_id = $1 AND id = $2
            RETURNING issue_counter
            """,
            workspace_id,
            team_id,
        )

        if number is None:
            return None

        # Annotated rather than returned inline: asyncpg ships no types, so
        # `fetchval` is Any and would silently satisfy any return type.
        allocated: int = number

        return allocated

    @staticmethod
    def _to_team(row: asyncpg.Record) -> TeamEntity:
        return TeamEntity(
            id=row["id"],
            workspace_id=row["workspace_id"],
            key=row["key"],
            name=row["name"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _to_workflow_state(row: asyncpg.Record) -> WorkflowStateEntity:
        return WorkflowStateEntity(
            id=row["id"],
            workspace_id=row["workspace_id"],
            team_id=row["team_id"],
            # Constructed through the enum rather than passed through as a
            # string, so a category the domain does not know about fails
            # here -- at the one place that reads it out of a row -- rather
            # than flowing on as a str that every `is`-comparison in the
            # application quietly answers False for.
            category=WorkflowStateCategory(row["category"]),
            name=row["name"],
            position=row["position"],
            color=row["color"],
            created_at=row["created_at"],
        )
