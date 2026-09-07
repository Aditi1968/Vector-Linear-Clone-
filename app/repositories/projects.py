from collections.abc import Sequence
from datetime import date, datetime
from uuid import UUID

import asyncpg

from app.domain.projects import (
    ProjectDependencies,
    ProjectEntity,
    ProjectMilestoneEntity,
    ProjectUpdateEntity,
)
from app.domain.tenancy import WorkspaceScope


# `list[...]` is not spellable inside the class below, and the reason is worth
# knowing rather than working around twice.
#
# The class has a method called `list` -- the API's name, kept deliberately;
# see the note on ruff's `A` rules in pyproject.toml. A class body is an
# ordinary namespace evaluated top to bottom, so from that `def` onwards the
# name `list` in it IS the method. `-> list[ProjectMilestoneEntity]` on a
# method below it is then a subscript of a function: `TypeError: 'function'
# object is not subscriptable` at import time, and `Function ... is not valid
# as a type` from mypy.
#
# `from __future__ import annotations` is the tempting fix and the wrong one:
# it defers evaluation, so the import succeeds and mypy still cannot read the
# signature -- the same defect, now silent. An alias resolved out here, where
# `list` is still the builtin, fixes both halves.
#
# Only the milestone list needs one. Every other `list[...]` in the class sits
# above the `def list`, where the name still means the builtin -- which is a
# fact about the current ordering and not a rule, so a method moved below it
# needs this alias too.
Milestones = list[ProjectMilestoneEntity]
Updates = list[ProjectUpdateEntity]


# The advisory-lock class for project dependency writes. See
# `ProjectRepository.lock_dependencies` for what it serialises and why.
#
# Two arguments, not one. PostgreSQL's one-argument pg_advisory_xact_lock and
# its two-argument form occupy DIFFERENT lock spaces, so this cannot collide
# with `scripts.apply_migration.ADVISORY_LOCK_KEY`. A different number from
# `app.repositories.relations.PARENTING_LOCK_CLASS` and from
# `app.repositories.initiatives.INITIATIVE_PARENTING_LOCK_CLASS`, because the
# three guard three different graphs and have no reason to serialise against
# one another.
DEPENDENCY_LOCK_CLASS = 0x56504445

# The columns every project-update read returns.
#
# A constant rather than seven copies, for the reason
# `app.repositories.relations._ISSUE_COLUMNS` is one: the entity is built from
# these rows, and a column missing is a KeyError at runtime on whichever read
# happens not to be exercised.
_PROJECT_UPDATE_COLUMNS = """
    id,
    project_id,
    health,
    body,
    author_id,
    created_at
"""


class ProjectRepository:
    """SQL access for `projects`, `project_teams` and `project_milestones`.

    The repository receives a connection from the service layer. It never
    acquires connections, never touches the pool, and never owns a
    transaction. `asyncpg.Record` never escapes this class.

    Every statement is scoped to one workspace, and the scope arrives as a
    required keyword argument -- the shape IssueRepository establishes, for
    the reasons stated there. Two of them matter more here than they do for
    issues, because a project is reachable through three tables: a caller
    cannot get to any of them without having decided which tenant it is
    addressing, and `scope=` appears literally at every call site, so "does
    this query cross tenants" is answered by reading the call.

    Nothing here checks a project against its workspace before writing to a
    child table. Every foreign key migrations/009_projects.sql declares is
    composite over `workspace_id`, so PostgreSQL refuses a cross-workspace
    association as part of the statement itself. A SELECT-first check would
    be a second, weaker copy of that rule -- weaker because it is a separate
    statement the row can change between, and weaker because it would then be
    two places that have to agree.
    """

    # ----------------------------------------------------------------- reads

    async def get_by_id(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        project_id: UUID,
    ) -> ProjectEntity | None:
        """The project with this id in this workspace, or nothing.

        The workspace is part of the lookup rather than a check applied
        afterwards, so a project belonging to another tenant produces exactly
        the same answer as an id that exists nowhere. A caller holding a
        guessed or leaked id learns nothing by asking.

        The team ids come back in the same statement. They are bound to the
        same `$1`/`$2` the outer predicate uses rather than correlated to the
        outer row, which is what makes this a single index lookup on
        project_teams_pkey's leading columns.
        """
        row = await connection.fetchrow(
            """
            SELECT
                id,
                name,
                description,
                state,
                health,
                target_date,
                lead_id,
                created_at,
                updated_at,
                (
                    SELECT COALESCE(
                        array_agg(pt.team_id ORDER BY pt.team_id),
                        ARRAY[]::UUID[]
                    )
                    FROM project_teams pt
                    WHERE pt.workspace_id = $1 AND pt.project_id = $2
                ) AS team_ids
            FROM projects
            WHERE workspace_id = $1 AND id = $2
            """,
            scope.workspace_id,
            project_id,
        )

        if row is None:
            return None

        return self._to_project(row)

    async def get_many_by_ids(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        project_ids: Sequence[UUID],
    ) -> list[ProjectEntity]:
        """Every project from this workspace whose id is in the list.

        The batching half of `Issue.project`. Ids the workspace does not own
        are simply absent from the result -- not an error and not a hole the
        caller can distinguish from an id that exists nowhere, which is the
        same property `get_by_id` has and for the same reason.

        `= ANY($2)` rather than an IN list built by string interpolation: the
        array is one bound parameter whatever its length, so the statement
        text is constant and there is nothing for a caller to inject into.
        """
        rows = await connection.fetch(
            """
            SELECT
                id,
                name,
                description,
                state,
                health,
                target_date,
                lead_id,
                created_at,
                updated_at,
                (
                    SELECT COALESCE(
                        array_agg(pt.team_id ORDER BY pt.team_id),
                        ARRAY[]::UUID[]
                    )
                    FROM project_teams pt
                    WHERE pt.workspace_id = projects.workspace_id
                        AND pt.project_id = projects.id
                ) AS team_ids
            FROM projects
            WHERE workspace_id = $1 AND id = ANY($2::UUID[])
            """,
            scope.workspace_id,
            list(project_ids),
        )

        return [self._to_project(row) for row in rows]

    async def search(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        query: str,
        limit: int,
    ) -> list[ProjectEntity]:
        """The workspace's projects matching a free-text query, best first.

        The same statement shape as `IssueRepository.search`, and that
        docstring carries the argument for every part of it: why
        `websearch_to_tsquery` rather than `to_tsquery`, why the tsquery
        expression is written twice rather than joined in, why the
        configuration is named, and why `id DESC` follows the rank.

        No `archived_at` predicate, because `projects` has no such column.
        Nothing here filters on `state` either: a completed project is still
        one a workspace searches for, and deciding otherwise is a product
        choice for the caller rather than a fact about this table.
        """
        rows = await connection.fetch(
            """
            SELECT
                id,
                name,
                description,
                state,
                health,
                target_date,
                lead_id,
                created_at,
                updated_at,
                (
                    SELECT COALESCE(
                        array_agg(pt.team_id ORDER BY pt.team_id),
                        ARRAY[]::UUID[]
                    )
                    FROM project_teams pt
                    WHERE pt.workspace_id = projects.workspace_id
                        AND pt.project_id = projects.id
                ) AS team_ids
            FROM projects
            WHERE workspace_id = $1
                AND search_vector @@ websearch_to_tsquery('english', $2)
            ORDER BY
                ts_rank(
                    search_vector,
                    websearch_to_tsquery('english', $2)
                ) DESC,
                id DESC
            LIMIT $3
            """,
            scope.workspace_id,
            query,
            limit,
        )

        return [self._to_project(row) for row in rows]

    async def list(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        limit: int,
        after_created_at: datetime | None,
        after_id: UUID | None,
    ) -> list[ProjectEntity]:
        """Keyset page of one workspace's projects, newest first.

        `limit` is expected to already be `first + 1` so the caller can detect
        a following page. No OFFSET: the cursor is a row-value comparison, and
        `workspace_id` leads both statements so a page is served by the
        leading columns of projects_workspace_created_at_id_idx.

        The tenant predicate is ANDed with the cursor rather than folded into
        it. Widening the row-value comparison to
        `(workspace_id, created_at, id) < (...)` would put workspaces into the
        ordering, which is how a page walk falls out of one tenant and into
        whichever one sorts next.

        The team-id aggregate is correlated here, because each row needs its
        own. That is one index-only lookup per project rather than a second
        round trip per project, which is the tradeoff worth taking for the one
        field this feature exists to expose.
        """
        if after_created_at is None or after_id is None:
            rows = await connection.fetch(
                """
                SELECT
                    id,
                    name,
                    description,
                    state,
                    health,
                    target_date,
                    lead_id,
                    created_at,
                    updated_at,
                    (
                        SELECT COALESCE(
                            array_agg(pt.team_id ORDER BY pt.team_id),
                            ARRAY[]::UUID[]
                        )
                        FROM project_teams pt
                        WHERE pt.workspace_id = projects.workspace_id
                            AND pt.project_id = projects.id
                    ) AS team_ids
                FROM projects
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
                    name,
                    description,
                    state,
                    health,
                    target_date,
                    lead_id,
                    created_at,
                    updated_at,
                    (
                        SELECT COALESCE(
                            array_agg(pt.team_id ORDER BY pt.team_id),
                            ARRAY[]::UUID[]
                        )
                        FROM project_teams pt
                        WHERE pt.workspace_id = projects.workspace_id
                            AND pt.project_id = projects.id
                    ) AS team_ids
                FROM projects
                WHERE workspace_id = $1 AND (created_at, id) < ($2, $3)
                ORDER BY created_at DESC, id DESC
                LIMIT $4
                """,
                scope.workspace_id,
                after_created_at,
                after_id,
                limit,
            )

        return [self._to_project(row) for row in rows]

    # ---------------------------------------------------------------- writes

    async def create(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        name: str,
        description: str | None,
        state: str,
        target_date: date | None,
        lead_id: UUID | None,
    ) -> ProjectEntity:
        """Insert one project into this workspace.

        `workspace_id` is written explicitly and carries no database default,
        so omitting it would be a NOT NULL violation rather than a quiet
        mis-filing. `state` likewise: the schema names the legal states and
        refuses everything else, but it does not choose one.

        `lead_id` is written into the same row as `workspace_id`, which is what
        makes `projects_lead_fk` a check worth having: both halves of that
        composite key come from this one statement, so a lead who is not a
        member of THIS workspace raises ForeignKeyViolationError here rather
        than being admitted by a SELECT the caller ran a moment earlier. There
        is no pre-check, deliberately -- see the class docstring.

        `team_ids` comes back as an empty array literal rather than from a
        query. A project one statement old has no `project_teams` rows -- no
        statement anywhere has been able to reference its id yet -- so a
        subquery here would be a round trip that can only ever return empty.
        """
        row = await connection.fetchrow(
            """
            INSERT INTO projects (
                workspace_id,
                name,
                description,
                state,
                target_date,
                lead_id
            )
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING
                id,
                name,
                description,
                state,
                health,
                target_date,
                lead_id,
                created_at,
                updated_at,
                ARRAY[]::UUID[] AS team_ids
            """,
            scope.workspace_id,
            name,
            description,
            state,
            target_date,
            lead_id,
        )

        return self._to_project(row)

    async def update(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        project_id: UUID,
        set_name: bool,
        name: str | None,
        set_description: bool,
        description: str | None,
        set_state: bool,
        state: str | None,
        set_target_date: bool,
        target_date: date | None,
        set_lead_id: bool,
        lead_id: UUID | None,
    ) -> ProjectEntity | None:
        """Apply a partial update, or return nothing if there is no such row.

        Each field arrives as a pair: a flag saying whether the caller
        mentioned it, and the value. That is what makes "clear the
        description" expressible at all -- `COALESCE($n, description)` cannot
        distinguish a NULL the caller asked for from a field it never
        mentioned, and would silently treat every clear as a no-op.

        One static statement rather than a SET list assembled per call. The
        SQL text is then the same for every combination of fields, so there is
        no place for a column name to arrive from anywhere but this file, and
        the server plans one statement instead of sixteen.

        Every value parameter is cast explicitly. Inside `CASE WHEN ... THEN
        $n ELSE column END` PostgreSQL would usually infer $n from the branch
        it sits beside, but a NULL parameter in an untyped position is exactly
        where that inference gets reported back as "could not determine data
        type", and a clear is the case that sends NULLs.

        Returning None means no row in THIS workspace has that id. The caller
        cannot tell that from "no such project anywhere", which is the point.

        Setting `lead_id` re-checks `projects_lead_fk` against the row's
        existing `workspace_id`, which this statement never touches -- so a
        lead from another tenant is refused here exactly as it is on insert.
        Clearing it (`set_lead_id` true, `lead_id` None) writes a NULL, and
        MATCH SIMPLE then exempts the row, which is what "this project has no
        lead" is stored as.
        """
        row = await connection.fetchrow(
            """
            UPDATE projects
            SET
                name = CASE WHEN $3::BOOLEAN THEN $4::TEXT ELSE name END,
                description = CASE
                    WHEN $5::BOOLEAN THEN $6::TEXT ELSE description
                END,
                state = CASE WHEN $7::BOOLEAN THEN $8::TEXT ELSE state END,
                target_date = CASE
                    WHEN $9::BOOLEAN THEN $10::DATE ELSE target_date
                END,
                lead_id = CASE WHEN $11::BOOLEAN THEN $12::UUID ELSE lead_id END,
                updated_at = now()
            WHERE workspace_id = $1 AND id = $2
            RETURNING
                id,
                name,
                description,
                state,
                health,
                target_date,
                lead_id,
                created_at,
                updated_at,
                (
                    SELECT COALESCE(
                        array_agg(pt.team_id ORDER BY pt.team_id),
                        ARRAY[]::UUID[]
                    )
                    FROM project_teams pt
                    WHERE pt.workspace_id = $1 AND pt.project_id = $2
                ) AS team_ids
            """,
            scope.workspace_id,
            project_id,
            set_name,
            name,
            set_description,
            description,
            set_state,
            state,
            set_target_date,
            target_date,
            set_lead_id,
            lead_id,
        )

        if row is None:
            return None

        return self._to_project(row)

    async def delete(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        project_id: UUID,
    ) -> bool:
        """Delete the project, reporting whether there was one to delete.

        The workspace is in the predicate, so this cannot reach another
        tenant's project however the id was obtained.

        This does NOT remove the rows that reference the project: every
        foreign key onto `projects` is ON DELETE RESTRICT, so a project still
        carrying teams, milestones or issues makes the server refuse this
        statement. ProjectService.delete clears them first, in one
        transaction. That ordering is deliberate -- see the note there on why
        the schema refuses to do it silently.
        """
        # Annotated rather than compared inline: asyncpg ships no types, so
        # `execute` is Any and `Any == str` would silently satisfy `bool`.
        status: str = await connection.execute(
            """
            DELETE FROM projects
            WHERE workspace_id = $1 AND id = $2
            """,
            scope.workspace_id,
            project_id,
        )

        return status == "DELETE 1"

    # ------------------------------------------------------------ team links

    async def add_team(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        project_id: UUID,
        team_id: UUID,
    ) -> None:
        """Associate one team with one project.

        The single `$1` feeding both foreign keys is the whole mechanism: the
        row carries one workspace, so the project and the team are checked
        against the same tenant. A team from another workspace raises
        ForeignKeyViolationError on `project_teams_team_fk`, a project from
        another workspace raises it on `project_teams_project_fk`, and a
        duplicate raises UniqueViolationError on `project_teams_pkey`. All
        three are expected outcomes of client input; ProjectService names them
        by constraint and translates them, and translates nothing else.

        No `ON CONFLICT DO NOTHING`. It would make this idempotent by hiding
        the one answer the caller might act on -- whether the association was
        already there -- and this repository has no way to report back what it
        chose to ignore.
        """
        await connection.execute(
            """
            INSERT INTO project_teams (workspace_id, project_id, team_id)
            VALUES ($1, $2, $3)
            """,
            scope.workspace_id,
            project_id,
            team_id,
        )

    async def remove_team(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        project_id: UUID,
        team_id: UUID,
    ) -> bool:
        """Dissociate one team, reporting whether it was associated.

        A project from another workspace matches nothing here, so the answer
        is the same `False` an unassociated team produces. That is the same
        indistinguishability every read in this class provides, on the write
        path.
        """
        # Annotated rather than compared inline: asyncpg ships no types, so
        # `execute` is Any and `Any == str` would silently satisfy `bool`.
        status: str = await connection.execute(
            """
            DELETE FROM project_teams
            WHERE workspace_id = $1 AND project_id = $2 AND team_id = $3
            """,
            scope.workspace_id,
            project_id,
            team_id,
        )

        return status == "DELETE 1"

    async def clear_teams(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        project_id: UUID,
    ) -> None:
        """Drop every team association of one project.

        Only used on the way to deleting the project. It reports no count,
        because there is nothing a caller could do differently for zero.
        """
        await connection.execute(
            """
            DELETE FROM project_teams
            WHERE workspace_id = $1 AND project_id = $2
            """,
            scope.workspace_id,
            project_id,
        )

    # ------------------------------------------------------------ milestones

    async def create_milestone(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        project_id: UUID,
        name: str,
        target_date: date | None,
    ) -> ProjectMilestoneEntity:
        """Append one milestone to the end of a project's list.

        The position is computed by the server inside the same statement, as
        `MAX(position) + 1` over the project's existing milestones. A
        SELECT-then-INSERT would leave a window in which another request
        appends and both writes land on the same number.

        This statement narrows that window rather than closing it: under READ
        COMMITTED two concurrent appends can still read the same maximum. That
        is survivable BY DESIGN and not by luck -- `position` carries no
        UNIQUE constraint, and every ordering of milestones breaks ties with
        `id`, so two milestones sharing a position are ordered stably rather
        than ambiguously. Making the column unique would turn this benign race
        into a failed mutation and every reorder into a shuffle.

        The aggregate over an empty set yields one row holding NULL, so a
        project with no milestones starts at 0 and the INSERT always writes
        exactly one row. A project that does not exist -- or belongs to
        another workspace -- also produces one row, and it is
        `project_milestones_project_fk` that refuses it. There is no
        pre-check.
        """
        row = await connection.fetchrow(
            """
            INSERT INTO project_milestones (
                workspace_id,
                project_id,
                name,
                target_date,
                position
            )
            SELECT
                $1,
                $2,
                $3,
                $4,
                COALESCE(MAX(existing.position) + 1, 0)
            FROM project_milestones existing
            WHERE existing.workspace_id = $1 AND existing.project_id = $2
            RETURNING
                id,
                project_id,
                name,
                target_date,
                position,
                created_at,
                updated_at
            """,
            scope.workspace_id,
            project_id,
            name,
            target_date,
        )

        return self._to_milestone(row)

    async def update_milestone(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        milestone_id: UUID,
        set_name: bool,
        name: str | None,
        set_target_date: bool,
        target_date: date | None,
        set_position: bool,
        position: int | None,
    ) -> ProjectMilestoneEntity | None:
        """Apply a partial update to one milestone, or report no such row.

        Flag-and-value pairs, one static statement and explicit casts, for the
        reasons given on `update`. The project is not among the updatable
        fields: moving a milestone between projects would silently invalidate
        `issues_milestone_fk` for every issue pointing at it, and the server
        would refuse the update with a message about issues rather than about
        the milestone. If that operation is ever wanted it needs its own
        method and its own decision about what happens to those issues.
        """
        row = await connection.fetchrow(
            """
            UPDATE project_milestones
            SET
                name = CASE WHEN $3::BOOLEAN THEN $4::TEXT ELSE name END,
                target_date = CASE
                    WHEN $5::BOOLEAN THEN $6::DATE ELSE target_date
                END,
                position = CASE
                    WHEN $7::BOOLEAN THEN $8::INTEGER ELSE position
                END,
                updated_at = now()
            WHERE workspace_id = $1 AND id = $2
            RETURNING
                id,
                project_id,
                name,
                target_date,
                position,
                created_at,
                updated_at
            """,
            scope.workspace_id,
            milestone_id,
            set_name,
            name,
            set_target_date,
            target_date,
            set_position,
            position,
        )

        if row is None:
            return None

        return self._to_milestone(row)

    async def list_milestones(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        project_id: UUID,
        limit: int,
    ) -> Milestones:
        """One project's milestones, in display order.

        Not paginated, and that is a product statement rather than an
        oversight: a milestone list is a handful of rows a client renders
        whole, so a cursor would buy a second round trip for every project
        page and a `hasNextPage` nobody reads.

        `limit` is required rather than defaulted, so the bound is a decision
        the service states out loud; see MILESTONE_LIST_LIMIT for what it is
        and why an unpaginated list needs one at all.

        `ORDER BY position, id` -- `id` because `position` is not unique, so
        without it two milestones sharing a number come back in whatever order
        the scan produced, differently between calls and between replicas. It
        is also what makes the truncation deterministic: without a total order
        the rows the limit keeps would vary between calls.
        """
        rows = await connection.fetch(
            """
            SELECT
                id,
                project_id,
                name,
                target_date,
                position,
                created_at,
                updated_at
            FROM project_milestones
            WHERE workspace_id = $1 AND project_id = $2
            ORDER BY position, id
            LIMIT $3
            """,
            scope.workspace_id,
            project_id,
            limit,
        )

        return [self._to_milestone(row) for row in rows]

    async def list_milestones_for_projects(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        project_ids: Sequence[UUID],
        limit_per_project: int,
    ) -> Milestones:
        """Every listed project's milestones, in one statement.

        The batching half of `Project.milestones`. Ordered by project first so
        the caller can group without re-sorting, then by the same
        `(position, id)` `list_milestones` uses -- one ordering rule for
        milestones, stated in two places that this file keeps identical.

        The limit is PER PROJECT, which is why it is a window function and not
        a `LIMIT`. A plain limit over the result would divide one budget among
        however many projects were on the page, so a project would come back
        whole when read alone and truncated when read in a list -- and which
        projects lost rows would depend on the page they landed on. Ranking
        inside `PARTITION BY project_id` gives each project the same bound it
        gets from `list_milestones`.

        `ORDER BY position, id` inside the window matches the outer ordering
        exactly. If the two disagreed the rows kept would not be the rows
        shown first, which is the subtle way a per-partition limit goes wrong.
        """
        rows = await connection.fetch(
            """
            SELECT
                id,
                project_id,
                name,
                target_date,
                position,
                created_at,
                updated_at
            FROM (
                SELECT
                    id,
                    project_id,
                    name,
                    target_date,
                    position,
                    created_at,
                    updated_at,
                    row_number() OVER (
                        PARTITION BY project_id ORDER BY position, id
                    ) AS rank
                FROM project_milestones
                WHERE workspace_id = $1 AND project_id = ANY($2::UUID[])
            ) ranked
            WHERE rank <= $3
            ORDER BY project_id, position, id
            """,
            scope.workspace_id,
            list(project_ids),
            limit_per_project,
        )

        return [self._to_milestone(row) for row in rows]

    async def delete_milestone(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        milestone_id: UUID,
    ) -> bool:
        """Delete one milestone, reporting whether there was one to delete.

        `issues_milestone_fk` is ON DELETE RESTRICT, so a milestone still
        carrying issues makes the server refuse this. The service clears those
        issues first, in the same transaction.
        """
        # Annotated rather than compared inline: asyncpg ships no types, so
        # `execute` is Any and `Any == str` would silently satisfy `bool`.
        status: str = await connection.execute(
            """
            DELETE FROM project_milestones
            WHERE workspace_id = $1 AND id = $2
            """,
            scope.workspace_id,
            milestone_id,
        )

        return status == "DELETE 1"

    async def delete_milestones_for_project(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        project_id: UUID,
    ) -> None:
        """Drop every milestone of one project, on the way to deleting it."""
        await connection.execute(
            """
            DELETE FROM project_milestones
            WHERE workspace_id = $1 AND project_id = $2
            """,
            scope.workspace_id,
            project_id,
        )

    # --------------------------------------------------------------- updates

    async def create_update(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        project_id: UUID,
        health: str,
        body: str,
        author_id: UUID,
    ) -> ProjectUpdateEntity:
        """Append one update to a project's history.

        Neither the project nor the author is read first.
        `project_updates_project_fk` and `project_updates_author_fk` are both
        composite over the same `workspace_id`, so a project from another
        tenant and an author who is not a member of this one are both refused
        by the statement that would have written the row -- with no window
        between a check and a write for a membership to be revoked in.
        """
        row = await connection.fetchrow(
            f"""
            INSERT INTO project_updates (
                workspace_id, project_id, health, body, author_id
            )
            VALUES ($1, $2, $3, $4, $5)
            RETURNING {_PROJECT_UPDATE_COLUMNS}
            """,
            scope.workspace_id,
            project_id,
            health,
            body,
            author_id,
        )

        return self._to_update(row)

    async def set_health(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        project_id: UUID,
        health: str,
    ) -> None:
        """Stamp the current health onto the project row.

        Called by the service in the same transaction as the update row that
        reported it, and never on its own -- see the long note on
        `projects.health` in migrations/022_initiatives.sql for why both the
        column and the log exist, and what keeps them in step.

        Reports nothing. A row that does not match is not a case a caller can
        act on: the insert of the update row has already run in this
        transaction and its foreign key would have refused a project that is
        not in this workspace.
        """
        await connection.execute(
            """
            UPDATE projects
            SET health = $3, updated_at = now()
            WHERE workspace_id = $1 AND id = $2
            """,
            scope.workspace_id,
            project_id,
            health,
        )

    async def list_updates(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        project_id: UUID,
        limit: int,
    ) -> Updates:
        """One project's update history, newest first.

        `(created_at DESC, id DESC)` and not `created_at` alone. Two updates
        posted in one transaction share a timestamp, and without the tie-break
        "which is the latest" would depend on the scan order -- which is the
        subtle way the health this history summarises comes out wrong.
        """
        rows = await connection.fetch(
            f"""
            SELECT {_PROJECT_UPDATE_COLUMNS}
            FROM project_updates
            WHERE workspace_id = $1 AND project_id = $2
            ORDER BY created_at DESC, id DESC
            LIMIT $3
            """,
            scope.workspace_id,
            project_id,
            limit,
        )

        return [self._to_update(row) for row in rows]

    async def list_updates_for_projects(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        project_ids: Sequence[UUID],
        limit_per_project: int,
    ) -> Updates:
        """Every listed project's updates, in one statement.

        The limit is PER PROJECT, which is why it is a window function and not
        a `LIMIT`, for the reason `list_milestones_for_projects` gives in full.
        """
        rows = await connection.fetch(
            f"""
            SELECT {_PROJECT_UPDATE_COLUMNS}
            FROM (
                SELECT
                    {_PROJECT_UPDATE_COLUMNS},
                    row_number() OVER (
                        PARTITION BY project_id
                        ORDER BY created_at DESC, id DESC
                    ) AS rank
                FROM project_updates
                WHERE workspace_id = $1 AND project_id = ANY($2::UUID[])
            ) ranked
            WHERE rank <= $3
            ORDER BY project_id, created_at DESC, id DESC
            """,
            scope.workspace_id,
            list(project_ids),
            limit_per_project,
        )

        return [self._to_update(row) for row in rows]

    async def clear_updates(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        project_id: UUID,
    ) -> None:
        """Drop one project's whole history, on the way to deleting it."""
        await connection.execute(
            """
            DELETE FROM project_updates
            WHERE workspace_id = $1 AND project_id = $2
            """,
            scope.workspace_id,
            project_id,
        )

    # ---------------------------------------------------------- dependencies

    async def lock_dependencies(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
    ) -> None:
        """Serialise dependency writes within one workspace.

        The whole of the dependency cycle guard's soundness, and the argument
        is `RelationRepository.lock_parenting`'s: `depends_on` reads a graph
        and `add_dependency` changes one, and between the two another
        transaction adding a different edge could invalidate what the first
        read -- the two edges together forming a cycle neither could see.

        Per workspace, not global, so tenants do not queue behind each other;
        `_xact_`, so the caller's commit or rollback releases it. A different
        lock class from initiative parenting, because the two guard different
        graphs and have no reason to queue behind each other.

        It binds only callers that take it. Any future writer of
        `project_dependencies` -- an import, an operator's INSERT -- can still
        write a cycle, and nothing here will notice.
        """
        await connection.execute(
            "SELECT pg_advisory_xact_lock($1, hashtext($2))",
            DEPENDENCY_LOCK_CLASS,
            str(scope.workspace_id),
        )

    async def depends_on(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        from_project_id: UUID,
        to_project_id: UUID,
    ) -> bool:
        """Whether `from_project_id` already reaches `to_project_id`.

        Asked before writing "A blocks B": the new edge closes a loop exactly
        when B already blocks, directly or through any chain, A. So the walk
        starts at B and follows the stored direction forward.

        Deliberately NOT depth-bounded, unlike the initiative hierarchy walk. A
        dependency chain has no product limit, so truncating this search would
        silently admit precisely the long cycles it exists to refuse -- an
        approval that is wrong, which is the one direction a bound must never
        fail in. Termination comes from `CYCLE ... SET ... USING ...` instead,
        which stops at the first repeated project, so a graph some other writer
        has already broken produces an answer rather than looping forever. The
        cost is bounded by the reachable sub-graph, and therefore by the number
        of projects in one workspace.

        The workspace predicate is on the recursive term as well as the anchor,
        so the walk cannot leave the tenant it started in.
        """
        reachable: bool = await connection.fetchval(
            """
            WITH RECURSIVE reachable (project_id) AS (
                SELECT blocked_project_id
                FROM project_dependencies
                WHERE workspace_id = $1 AND blocking_project_id = $2

                UNION ALL

                SELECT edge.blocked_project_id
                FROM project_dependencies edge
                JOIN reachable ON edge.blocking_project_id = reachable.project_id
                WHERE edge.workspace_id = $1
            ) CYCLE project_id SET is_cycle USING path
            SELECT EXISTS (
                SELECT 1 FROM reachable WHERE reachable.project_id = $3
            )
            """,
            scope.workspace_id,
            from_project_id,
            to_project_id,
        )

        return reachable

    async def add_dependency(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        blocking_project_id: UUID,
        blocked_project_id: UUID,
    ) -> None:
        """Record that one project blocks another.

        One row for the edge, stored in one direction only; `blocked_by` is
        this same row read from the other end and is produced by
        `list_dependencies`, never stored.

        Neither project is read first. Both foreign keys read the row's single
        `workspace_id`, so a project from another workspace raises
        ForeignKeyViolationError on whichever of
        `project_dependencies_blocking_fk` / `project_dependencies_blocked_fk`
        named it, a duplicate raises UniqueViolationError on
        `project_dependencies_pkey`, and a self-dependency that got past the
        service raises CheckViolationError on
        `project_dependencies_not_self`.
        """
        await connection.execute(
            """
            INSERT INTO project_dependencies (
                workspace_id, blocking_project_id, blocked_project_id
            )
            VALUES ($1, $2, $3)
            """,
            scope.workspace_id,
            blocking_project_id,
            blocked_project_id,
        )

    async def remove_dependency(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        blocking_project_id: UUID,
        blocked_project_id: UUID,
    ) -> bool:
        """Drop one dependency, reporting whether there was one to drop.

        A project from another workspace matches nothing here, so the answer is
        the same `False` an edge that never existed produces.
        """
        # Annotated rather than compared inline: asyncpg ships no types, so
        # `execute` is Any and `Any == str` would silently satisfy `bool`.
        status: str = await connection.execute(
            """
            DELETE FROM project_dependencies
            WHERE workspace_id = $1
                AND blocking_project_id = $2
                AND blocked_project_id = $3
            """,
            scope.workspace_id,
            blocking_project_id,
            blocked_project_id,
        )

        return status == "DELETE 1"

    async def list_dependencies_for_projects(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        project_ids: Sequence[UUID],
    ) -> dict[UUID, ProjectDependencies]:
        """Both directions, for several projects, in one statement.

        One statement rather than two, and one aggregate per direction rather
        than a row per edge: reading a project's dependencies means finding
        rows where it is the blocking end OR the blocked end, and an OR across
        two columns cannot be served by one index. The two halves are two
        separate equality lookups -- one on project_dependencies_pkey's leading
        columns, one on project_dependencies_workspace_blocked_idx -- and each
        one aggregates to an array so the result is one row per project rather
        than one per edge.

        Not paginated, and that is a product statement: a project has a handful
        of dependencies and a client renders them whole. The bound is the
        caller's `project_ids` list, which is already a page.

        Returns a dict rather than a list, because the caller is a batch loader
        that has to pair answers to keys and a list would make it re-group.
        A project with no dependencies at either end is simply absent -- the
        same answer a project in another workspace gives.
        """
        rows = await connection.fetch(
            """
            SELECT
                subject.project_id,
                (
                    SELECT COALESCE(
                        array_agg(
                            outgoing.blocked_project_id
                            ORDER BY outgoing.blocked_project_id
                        ),
                        ARRAY[]::UUID[]
                    )
                    FROM project_dependencies outgoing
                    WHERE outgoing.workspace_id = $1
                        AND outgoing.blocking_project_id = subject.project_id
                ) AS blocks,
                (
                    SELECT COALESCE(
                        array_agg(
                            incoming.blocking_project_id
                            ORDER BY incoming.blocking_project_id
                        ),
                        ARRAY[]::UUID[]
                    )
                    FROM project_dependencies incoming
                    WHERE incoming.workspace_id = $1
                        AND incoming.blocked_project_id = subject.project_id
                ) AS blocked_by
            FROM unnest($2::UUID[]) AS subject (project_id)
            """,
            scope.workspace_id,
            list(project_ids),
        )

        return {
            row["project_id"]: ProjectDependencies(
                blocks=tuple(row["blocks"]),
                blocked_by=tuple(row["blocked_by"]),
            )
            for row in rows
        }

    async def clear_dependencies(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        project_id: UUID,
    ) -> None:
        """Drop every dependency at either end of one project.

        Both directions in one statement, on the way to deleting the project.
        Doing it in two would leave a window in which the project was half
        detached, and the second statement is the one that would fail.
        """
        await connection.execute(
            """
            DELETE FROM project_dependencies
            WHERE workspace_id = $1
                AND (blocking_project_id = $2 OR blocked_project_id = $2)
            """,
            scope.workspace_id,
            project_id,
        )

    # ---------------------------------------------------------------- mapping

    @staticmethod
    def _to_update(row: asyncpg.Record) -> ProjectUpdateEntity:
        return ProjectUpdateEntity(
            id=row["id"],
            project_id=row["project_id"],
            health=row["health"],
            body=row["body"],
            author_id=row["author_id"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _to_project(row: asyncpg.Record) -> ProjectEntity:
        # `team_ids` arrives as a Python list; the entity is frozen, so it is
        # copied into a tuple rather than handed out as a mutable alias of
        # whatever asyncpg built.
        return ProjectEntity(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            state=row["state"],
            health=row["health"],
            target_date=row["target_date"],
            lead_id=row["lead_id"],
            team_ids=tuple(row["team_ids"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _to_milestone(row: asyncpg.Record) -> ProjectMilestoneEntity:
        return ProjectMilestoneEntity(
            id=row["id"],
            project_id=row["project_id"],
            name=row["name"],
            target_date=row["target_date"],
            position=row["position"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
