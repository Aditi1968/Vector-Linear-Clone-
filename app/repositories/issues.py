from datetime import date, datetime
from uuid import UUID

import asyncpg

from app.domain.issues import (
    TERMINAL_STATE_CATEGORIES,
    UNSET,
    IssueEntity,
    IssuePatch,
    Unset,
)
from app.domain.tenancy import WorkspaceScope


def _value[T](field: T | Unset) -> T | None:
    """The value a patch field carries, with UNSET flattened to None.

    Every use of this is paired with a separate `is not UNSET` flag in the
    same statement, so the None this returns for an absent field is never
    read: the CASE it feeds takes its ELSE branch. Flattening rather than
    raising keeps the two parameters of a field independent -- the flag says
    whether to write, the value says what -- which is what lets one uniform
    pair of parameters serve a field whose real value may itself be None.
    """
    if isinstance(field, Unset):
        return None

    return field


# Every column an IssueEntity is built from, as one expression list shared by
# the statements below.
#
# This is interpolated into the SQL with an f-string, which in this repository
# needs a defence. "Parameterized SQL only" is a rule about *values*: a value
# reaching a statement by concatenation is an injection, which is why every
# one of them below arrives as $n. This constant is not a value. It is a
# module-level literal that no input can influence, chosen instead of five
# copies of the same seventeen lines because the copies are how a column gets
# added to the entity, to four statements, and forgotten in the fifth -- a
# mistake that surfaces as a KeyError from `_to_entity` at runtime, on
# whichever path happened not to be exercised.
#
# `team_key` is a correlated subquery rather than a join, deliberately. A join
# would restructure `list`, whose FROM and WHERE are the keyset walk that
# migrations 002 and 006 index and that this project's pagination suite reads
# statement-by-statement; a scalar subquery leaves that shape untouched and
# costs a primary-key lookup against a table with one row per team.
ISSUE_COLUMNS = """
                issues.id,
                issues.team_id,
                (
                    SELECT teams.key
                    FROM teams
                    WHERE teams.workspace_id = issues.workspace_id
                        AND teams.id = issues.team_id
                ) AS team_key,
                issues.number,
                issues.title,
                issues.description,
                issues.priority,
                issues.workflow_state_id,
                issues.assignee_id,
                issues.creator_id,
                issues.estimate,
                issues.due_date,
                issues.cycle_id,
                issues.project_id,
                issues.milestone_id,
                issues.completed_at,
                issues.archived_at,
                issues.created_at,
                issues.updated_at
"""


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

    The writes carry a second discipline, inherited from `create` and now
    applied to `update` as well: no statement here checks a tenant rule by
    reading first. An assignee from another workspace, a workflow state from
    another team, a team from another workspace -- each is refused by a
    composite foreign key as part of the same statement that would have
    written it. A SELECT beforehand would be a second copy of each of those
    rules, separated from the write by a round trip in which the fact it
    checked can change.

    Every read and every write carries `archived_at IS NULL`. Archiving
    removes an issue from the product, so a statement here that still
    resolved one would leave every caller to remember a filter this class
    can apply once.
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

        `archived_at IS NULL` joins that predicate rather than sitting
        beside it, so an archived issue is also the same answer.
        """
        row = await connection.fetchrow(
            f"""
            SELECT
{ISSUE_COLUMNS}
            FROM issues
            WHERE workspace_id = $1 AND id = $2 AND archived_at IS NULL
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
        assignee_id: UUID | None,
        creator_id: UUID | None,
        estimate: int | None,
        due_date: date | None,
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

        `completed_at` is not written, and must not be. The state a new
        issue starts in is non-terminal by construction -- the service
        resolves it through `TeamService.default_workflow_state_id`, which
        selects the `unstarted` category -- so the column's NULL default is
        already the answer the completed_at rule gives. Writing it here
        would put that rule in a second place; see `IssueService`.

        Neither the team, the assignee nor the workflow state is checked
        against the workspace before the insert. `issues_team_fk`,
        `issues_assignee_fk` and `issues_workflow_state_fk` are composite
        foreign keys onto `teams (workspace_id, id)`,
        `workspace_members (workspace_id, user_id)` and
        `workflow_states (workspace_id, team_id, id)` respectively, so the
        server refuses each mismatch as part of this statement. A SELECT
        here first would be a second, weaker copy of those rules: weaker
        because it is a separate statement, so a membership could be revoked
        between the two, and weaker because it would then be two places that
        have to agree. A mismatched pair therefore surfaces as
        ForeignKeyViolationError, which the service translates for the two
        constraints a client can actually provoke and lets propagate for the
        rest.

        `cycle_id` is not written and carries no default, so a new issue is
        in no cycle. Accepting one here would mean validating a cycle
        against a team the caller has only just named; `set_cycle` validates
        it against the team the database has already stored.
        """
        row = await connection.fetchrow(
            f"""
            INSERT INTO issues (
                workspace_id,
                team_id,
                number,
                workflow_state_id,
                title,
                description,
                priority,
                assignee_id,
                creator_id,
                estimate,
                due_date
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            RETURNING
{ISSUE_COLUMNS}
            """,
            scope.workspace_id,
            team_id,
            number,
            workflow_state_id,
            title,
            description,
            priority,
            assignee_id,
            creator_id,
            estimate,
            due_date,
        )

        return self._to_entity(row)

    async def update(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
        patch: IssuePatch,
    ) -> IssueEntity | None:
        """Apply a patch to one live issue in this workspace, or nothing.

        One statement, and that is the point of its shape. The alternative
        -- SELECT the row, merge in Python, UPDATE it back -- reads a value
        and writes a value derived from it across two round trips, so two
        concurrent edits each write a row computed from a state that is
        already stale by the time it lands, and the later write silently
        discards the earlier one. Here every field is either replaced by a
        bound parameter or left as the column's own value, evaluated by the
        server while it holds the row.

        Each field is `CASE WHEN <flag> THEN <value> ELSE <column> END` --
        two parameters per field rather than one. The flag is what
        distinguishes "set this to nothing" from "do not touch this", which
        a single nullable parameter cannot: without it, clearing an assignee
        and not mentioning the assignee are the same request. The uniform
        two-parameter shape is used even for the columns that are NOT NULL,
        where a bare NULL could have meant "unchanged" unambiguously, so
        that reading this statement needs no per-column reasoning about
        nullability and so that a column later becoming nullable does not
        silently change what an update means.

        Scoped exactly as the reads are. Updating an issue in another
        workspace matches no row and returns None, which is the same answer
        as an id that exists nowhere -- so, as with `get_by_id`, the
        operation cannot be used to discover that someone else's issue
        exists. `archived_at IS NULL` is in the predicate for the same
        reason it is in the reads: an archived issue is not there to edit.

        `completed_at` is assigned on every update rather than only when the
        state changes, and it is computed from the state the row will end up
        in -- see `IssueService` for the rule itself. Recomputing
        unconditionally is what makes the rule self-repairing: a row whose
        `completed_at` disagrees with its state, however it got that way, is
        corrected by the next write instead of carrying the disagreement
        forever.

        The workflow-state subquery is scoped to the workspace. Without that
        predicate a state id from another tenant would be read here to
        decide `completed_at`; the write would still be refused by
        `issues_workflow_state_fk`, so nothing would be corrupted, but the
        row would have been read, and a statement that reads another
        tenant's row is not one to leave in place because its result happens
        to be discarded.
        """
        row = await connection.fetchrow(
            f"""
            WITH target AS (
                SELECT
                    issues.id,
                    CASE
                        WHEN $9 THEN $10::UUID
                        ELSE issues.workflow_state_id
                    END AS workflow_state_id
                FROM issues
                WHERE issues.workspace_id = $1
                    AND issues.id = $2
                    AND issues.archived_at IS NULL
            )
            UPDATE issues
            SET
                title = CASE WHEN $3 THEN $4::TEXT ELSE issues.title END,
                description = CASE
                    WHEN $5 THEN $6::TEXT
                    ELSE issues.description
                END,
                priority = CASE
                    WHEN $7 THEN $8::SMALLINT
                    ELSE issues.priority
                END,
                workflow_state_id = target.workflow_state_id,
                assignee_id = CASE
                    WHEN $11 THEN $12::UUID
                    ELSE issues.assignee_id
                END,
                estimate = CASE
                    WHEN $13 THEN $14::INTEGER
                    ELSE issues.estimate
                END,
                due_date = CASE
                    WHEN $15 THEN $16::DATE
                    ELSE issues.due_date
                END,
                completed_at = CASE
                    WHEN (
                        SELECT workflow_states.type
                        FROM workflow_states
                        WHERE workflow_states.id = target.workflow_state_id
                            AND workflow_states.workspace_id = $1
                    ) = ANY($17::TEXT[])
                    THEN COALESCE(issues.completed_at, now())
                    ELSE NULL
                END,
                updated_at = now()
            FROM target
            WHERE issues.workspace_id = $1
                AND issues.id = target.id
                AND issues.archived_at IS NULL
            RETURNING
{ISSUE_COLUMNS}
            """,
            scope.workspace_id,
            issue_id,
            patch.title is not UNSET,
            _value(patch.title),
            patch.description is not UNSET,
            _value(patch.description),
            patch.priority is not UNSET,
            _value(patch.priority),
            patch.workflow_state_id is not UNSET,
            _value(patch.workflow_state_id),
            patch.assignee_id is not UNSET,
            _value(patch.assignee_id),
            patch.estimate is not UNSET,
            _value(patch.estimate),
            patch.due_date is not UNSET,
            _value(patch.due_date),
            list(TERMINAL_STATE_CATEGORIES),
        )

        if row is None:
            return None

        return self._to_entity(row)

    async def archive(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
    ) -> IssueEntity | None:
        """Take one issue off the board, or answer nothing.

        `archived_at IS NULL` in the predicate makes this match nothing
        rather than succeed on an already-archived issue, which keeps the
        timestamp meaning "when this was archived" instead of "when it was
        last archived again". The caller cannot tell that case from a
        nonexistent id or another tenant's -- all three are None -- and that
        is correct rather than merely convenient: an archived issue is
        invisible to every read on this class, so a caller who cannot see it
        must not be able to learn it exists by trying to archive it.

        The row is returned rather than a row count. An archive is a state
        change the client has to render, and returning the issue lets it do
        that from the mutation's own result instead of refetching a row
        that, by then, no read here will hand back.
        """
        row = await connection.fetchrow(
            f"""
            UPDATE issues
            SET archived_at = now(),
                updated_at = now()
            WHERE workspace_id = $1
                AND id = $2
                AND archived_at IS NULL
            RETURNING
{ISSUE_COLUMNS}
            """,
            scope.workspace_id,
            issue_id,
        )

        if row is None:
            return None

        return self._to_entity(row)

    async def list(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        team_id: UUID | None,
        limit: int,
        after_created_at: datetime | None,
        after_id: UUID | None,
    ) -> list[IssueEntity]:
        """Keyset page of one workspace's live issues, newest first.

        `limit` is expected to already be `first + 1` so the caller can
        detect a following page. No OFFSET: the cursor is a row-value
        comparison, and `workspace_id` leads both statements so a page is
        served by the leading columns of
        issues_workspace_live_created_at_id_idx.

        The tenant predicate is ANDed with the cursor rather than folded
        into it. Widening the row-value comparison to
        `(workspace_id, created_at, id) < (...)` would put workspaces into
        the ordering, which is how a page walk falls out of one tenant and
        into whichever one sorts next; the workspace is an equality and only
        `(created_at, id)` is the keyset.

        `archived_at IS NULL` is ANDed on for exactly the same reason and
        with the same care -- it is another equality-shaped filter, not part
        of the ordering key. Migration 006 adds a partial index carrying
        that predicate, so archived rows are absent from the index this walk
        reads rather than fetched and discarded; without it the cost of a
        page would grow with every issue the workspace had ever created
        instead of with the ones still on its board.

        `team_id` is an optional NARROWING and never a widening. It is ANDed
        on alongside the workspace, in that order, so a team id belonging to
        another workspace intersects with nothing rather than selecting that
        workspace's rows -- the client-supplied id can only ever remove rows
        the tenant predicate already admitted. It is bound as one parameter
        with a NULL-means-all test rather than by building two statements,
        because a filter that is absent must not change the plan the keyset
        walk uses.
        """
        if after_created_at is None or after_id is None:
            rows = await connection.fetch(
                f"""
                SELECT
{ISSUE_COLUMNS}
                FROM issues
                WHERE workspace_id = $1
                    AND ($2::UUID IS NULL OR team_id = $2)
                    AND archived_at IS NULL
                ORDER BY created_at DESC, id DESC
                LIMIT $3
                """,
                scope.workspace_id,
                team_id,
                limit,
            )
        else:
            rows = await connection.fetch(
                f"""
                SELECT
{ISSUE_COLUMNS}
                FROM issues
                WHERE workspace_id = $1
                    AND ($2::UUID IS NULL OR team_id = $2)
                    AND (created_at, id) < ($3, $4)
                    AND archived_at IS NULL
                ORDER BY created_at DESC, id DESC
                LIMIT $5
                """,
                scope.workspace_id,
                team_id,
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
            f"""
            UPDATE issues
            SET cycle_id = $3,
                updated_at = now()
            WHERE workspace_id = $1 AND id = $2
            RETURNING
{ISSUE_COLUMNS}
            """,
            scope.workspace_id,
            issue_id,
            cycle_id,
        )

        if row is None:
            return None

        return self._to_entity(row)

    async def set_project(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
        project_id: UUID | None,
        milestone_id: UUID | None,
    ) -> IssueEntity | None:
        """Place one issue in a project and milestone, or take it out of both.

        Both columns are written together on purpose. They are not two
        independent settings: `issues_milestone_requires_project` refuses a
        milestone without a project, and `issues_milestone_fk` refuses a
        milestone belonging to a different project than the one on the same
        row. A method that set them one at a time would have to pass through a
        state the schema forbids in order to reach a state it allows, so the
        write is a single statement that moves the issue from one legal pair
        to another.

        Nothing is checked before the UPDATE. The three ways this can be
        wrong -- a project from another workspace, a milestone from another
        project, a milestone with no project -- are the three constraints
        migrations/009_projects.sql declares, and each surfaces as a
        PostgresError naming the constraint it broke. IssueService translates
        exactly those names and nothing else.

        Returning None means no row in this workspace has that id, which is
        the same answer another tenant's issue produces.
        """
        row = await connection.fetchrow(
            f"""
            UPDATE issues
            SET
                project_id = $3,
                milestone_id = $4,
                updated_at = now()
            WHERE workspace_id = $1 AND id = $2
            RETURNING
{ISSUE_COLUMNS}
            """,
            scope.workspace_id,
            issue_id,
            project_id,
            milestone_id,
        )

        if row is None:
            return None

        return self._to_entity(row)

    async def clear_project(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        project_id: UUID,
    ) -> None:
        """Detach every issue from one project, milestones included.

        Called on the way to deleting the project. `milestone_id` is cleared
        in the same statement because every milestone of that project is about
        to go too, and an issue left pointing at one would make
        `issues_milestone_requires_project` false the instant `project_id`
        became NULL -- so clearing only the project is a state the server
        would refuse, not merely one that would look odd.
        """
        await connection.execute(
            """
            UPDATE issues
            SET project_id = NULL, milestone_id = NULL, updated_at = now()
            WHERE workspace_id = $1 AND project_id = $2
            """,
            scope.workspace_id,
            project_id,
        )

    async def clear_milestone(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        milestone_id: UUID,
    ) -> None:
        """Detach every issue from one milestone, leaving them in the project.

        The mirror image of `clear_project`, and deliberately not symmetrical
        with it: deleting a milestone is a change to the plan inside a
        project, not a removal of the work from it.
        """
        await connection.execute(
            """
            UPDATE issues
            SET milestone_id = NULL, updated_at = now()
            WHERE workspace_id = $1 AND milestone_id = $2
            """,
            scope.workspace_id,
            milestone_id,
        )

    @staticmethod
    def _to_entity(row: asyncpg.Record) -> IssueEntity:
        return IssueEntity(
            id=row["id"],
            team_id=row["team_id"],
            team_key=row["team_key"],
            number=row["number"],
            title=row["title"],
            description=row["description"],
            priority=row["priority"],
            workflow_state_id=row["workflow_state_id"],
            assignee_id=row["assignee_id"],
            creator_id=row["creator_id"],
            estimate=row["estimate"],
            due_date=row["due_date"],
            cycle_id=row["cycle_id"],
            project_id=row["project_id"],
            milestone_id=row["milestone_id"],
            completed_at=row["completed_at"],
            archived_at=row["archived_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
