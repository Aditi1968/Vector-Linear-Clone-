from datetime import datetime
from uuid import UUID

import asyncpg

from app.domain.issues import IssueEntity
from app.domain.tenancy import WorkspaceScope


class IssueRepository:
    """SQL access for `issues`.

    The repository receives a connection from the service layer. It never
    acquires connections, never touches the pool, and never owns a
    transaction. `asyncpg.Record` never escapes this class.

    Every statement here is scoped to one workspace, and the scope arrives
    as a required keyword argument -- never a default, never an attribute
    set once on the instance. Two things follow, and both are the reason for
    the shape. A caller cannot reach this class without having decided which
    tenant it is addressing, because no signature here omits the question.
    And `scope=` appears literally at every call site, so "does this query
    cross tenants" is answered by reading the call rather than by tracing
    back to whatever constructed the repository.
    """

    async def get_by_id(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
    ) -> IssueEntity | None:
        """The issue with this id in this workspace, or nothing.

        The workspace is part of the lookup, not a check applied to the row
        afterwards. That distinction is the security property: an issue
        belonging to another tenant produces exactly the same answer as an
        id that exists nowhere at all, so a caller holding a guessed or
        leaked id learns nothing by asking. Fetching by id first and then
        comparing `row["workspace_id"]` would return the same None while
        having already read another tenant's row into this process.
        """
        row = await connection.fetchrow(
            """
            SELECT
                id,
                title,
                description,
                priority,
                cycle_id,
                completed_at,
                created_at,
                updated_at
            FROM issues
            WHERE workspace_id = $1 AND id = $2
            """,
            scope.workspace_id,
            issue_id,
        )

        if row is None:
            return None

        return self._to_entity(row)

    async def create(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        team_id: UUID,
        number: int,
        workflow_state_id: UUID,
        title: str,
        description: str | None,
        priority: int,
    ) -> IssueEntity:
        """Insert one issue into this workspace, against this team.

        `number` and `workflow_state_id` are supplied by the caller rather
        than defaulted here, and both are required for the same reason the
        tenancy columns are: 005 made them NOT NULL with no default, so a
        caller that forgets one is refused by the database instead of
        writing a row with an invented identifier or an unset status. The
        number in particular has to come from the caller, because it must be
        allocated inside the same transaction as this insert -- see
        `TeamService.allocate_issue_number`.

        Both tenancy columns are written explicitly. Neither carries a
        database default, deliberately -- migrations/002_tenancy.sql:102-109
        argues that a default would turn every insert that forgot a
        workspace into a silent write against the bootstrap tenant instead
        of a loud rejection -- so omitting either here is a NOT NULL
        violation rather than a quiet mis-filing.

        The team is not checked against the workspace before the insert.
        `issues_team_fk` is a composite foreign key onto
        `teams (workspace_id, id)`, so the server refuses a team belonging
        to another workspace as part of this statement. A SELECT here first
        would be a second, weaker copy of that rule: weaker because it is a
        separate statement, so the team could be moved between the two, and
        weaker because it would then be two places that have to agree. A
        mismatched pair therefore surfaces as ForeignKeyViolationError,
        which is a defect in the caller rather than user input, and is left
        to propagate as one.

        `cycle_id` is not written and carries no default, so a new issue is
        in no cycle. Accepting one here would mean validating a cycle
        against a team the caller has only just named; `set_cycle` validates
        it against the team the database has already stored.
        """
        row = await connection.fetchrow(
            """
            INSERT INTO issues (
                workspace_id,
                team_id,
                number,
                workflow_state_id,
                title,
                description,
                priority
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING
                id,
                title,
                description,
                priority,
                cycle_id,
                completed_at,
                created_at,
                updated_at
            """,
            scope.workspace_id,
            team_id,
            number,
            workflow_state_id,
            title,
            description,
            priority,
        )

        return self._to_entity(row)

    async def list(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        limit: int,
        after_created_at: datetime | None,
        after_id: UUID | None,
    ) -> list[IssueEntity]:
        """Keyset page of one workspace's issues, newest first.

        `limit` is expected to already be `first + 1` so the caller can
        detect a following page. No OFFSET: the cursor is a row-value
        comparison, and `workspace_id` leads both statements so a page is
        served by the leading columns of
        issues_workspace_created_at_id_idx.

        The tenant predicate is ANDed with the cursor rather than folded
        into it. Widening the row-value comparison to
        `(workspace_id, created_at, id) < (...)` would put workspaces into
        the ordering, which is how a page walk falls out of one tenant and
        into whichever one sorts next; the workspace is an equality and only
        `(created_at, id)` is the keyset.
        """
        if after_created_at is None or after_id is None:
            rows = await connection.fetch(
                """
                SELECT
                    id,
                    title,
                    description,
                    priority,
                    cycle_id,
                    completed_at,
                    created_at,
                    updated_at
                FROM issues
                WHERE workspace_id = $1
                ORDER BY created_at DESC, id DESC
                LIMIT $2
                """,
                scope.workspace_id,
                limit,
            )
        else:
            rows = await connection.fetch(
                """
                SELECT
                    id,
                    title,
                    description,
                    priority,
                    cycle_id,
                    completed_at,
                    created_at,
                    updated_at
                FROM issues
                WHERE workspace_id = $1 AND (created_at, id) < ($2, $3)
                ORDER BY created_at DESC, id DESC
                LIMIT $4
                """,
                scope.workspace_id,
                after_created_at,
                after_id,
                limit,
            )

        return [self._to_entity(row) for row in rows]

    async def set_cycle(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
        cycle_id: UUID | None,
    ) -> IssueEntity | None:
        """Move one issue into a cycle, or out of whatever cycle it is in.

        None for `cycle_id` is the unassignment, and it is a real value here
        rather than a skipped column: `SET cycle_id = $3` with a NULL bound
        to it is one statement whether the issue is joining a cycle or
        leaving one.

        Nothing checks that the cycle belongs to this issue's team, and
        nothing may. `issues_cycle_fk` references
        `cycles (workspace_id, team_id, id)` using the issue's OWN stored
        workspace and team, so a cycle from another team -- or another
        tenant -- is refused by the server as part of this statement, and it
        is refused against the issue's committed team rather than against
        whatever a preceding SELECT read. A pre-check would be a second,
        weaker copy of that rule: weaker because the issue could be moved to
        another team between the two statements, and weaker because two
        places that have to agree eventually will not. The violation reaches
        IssueService, which is the layer that decides what a client is told.

        `updated_at` is stamped here because the issue changed; the column
        has no trigger behind it.

        None means no row matched the id in this workspace -- the same
        answer for an issue that does not exist and one belonging to another
        tenant.
        """
        row = await connection.fetchrow(
            """
            UPDATE issues
            SET cycle_id = $3,
                updated_at = now()
            WHERE workspace_id = $1 AND id = $2
            RETURNING
                id,
                title,
                description,
                priority,
                cycle_id,
                completed_at,
                created_at,
                updated_at
            """,
            scope.workspace_id,
            issue_id,
            cycle_id,
        )

        if row is None:
            return None

        return self._to_entity(row)

    @staticmethod
    def _to_entity(row: asyncpg.Record) -> IssueEntity:
        return IssueEntity(
            id=row["id"],
            title=row["title"],
            description=row["description"],
            priority=row["priority"],
            cycle_id=row["cycle_id"],
            completed_at=row["completed_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
