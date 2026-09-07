from collections.abc import Sequence
from datetime import date, datetime
from uuid import UUID

import asyncpg

from app.domain.initiatives import (
    InitiativeEntity,
    InitiativeUpdateEntity,
    ParentingCheck,
)
from app.domain.tenancy import WorkspaceScope


# `list[...]` is not spellable inside the class below, and the reason is the
# one app/repositories/projects.py states in full: the class has a method
# called `list`, so from that `def` onwards the name `list` in the class body
# IS the method, and `-> list[InitiativeUpdateEntity]` on a method below it is
# a subscript of a function. Aliases resolved out here, where `list` is still
# the builtin, fix both the import-time TypeError and mypy.
Updates = list[InitiativeUpdateEntity]
Initiatives = list[InitiativeEntity]

# The advisory-lock class for initiative re-parenting. See
# `InitiativeRepository.lock_parenting` for what it serialises and why.
#
# Two arguments, not one. PostgreSQL's one-argument pg_advisory_xact_lock and
# its two-argument form occupy DIFFERENT lock spaces, so this cannot collide
# with `scripts.apply_migration.ADVISORY_LOCK_KEY`. It is a different number
# from `app.repositories.relations.PARENTING_LOCK_CLASS` because the two guard
# different graphs: re-parenting an issue and re-parenting an initiative have
# no reason to queue behind each other.
INITIATIVE_PARENTING_LOCK_CLASS = 0x56494E49

# The columns every initiative read returns, including the two aggregates the
# entity carries.
#
# Correlated to the outer row rather than bound to the statement's parameters,
# so one constant serves the single-row reads and the list alike. On a
# single-row read that costs nothing measurable -- the correlation resolves to
# the same one index lookup -- and it buys there being one definition of what
# an initiative row is, which is what stops a column added to the entity from
# being a KeyError on whichever read nobody exercised.
_INITIATIVE_COLUMNS = """
    initiatives.id,
    initiatives.name,
    initiatives.description,
    initiatives.status,
    initiatives.health,
    initiatives.target_date,
    initiatives.owner_id,
    initiatives.parent_initiative_id,
    initiatives.created_at,
    initiatives.updated_at,
    (
        SELECT COALESCE(
            array_agg(ip.project_id ORDER BY ip.project_id),
            ARRAY[]::UUID[]
        )
        FROM initiative_projects ip
        WHERE ip.workspace_id = initiatives.workspace_id
            AND ip.initiative_id = initiatives.id
    ) AS project_ids,
    (
        SELECT COALESCE(
            array_agg(child.id ORDER BY child.id),
            ARRAY[]::UUID[]
        )
        FROM initiatives child
        WHERE child.workspace_id = initiatives.workspace_id
            AND child.parent_initiative_id = initiatives.id
    ) AS child_initiative_ids
"""

_UPDATE_COLUMNS = """
    id,
    initiative_id,
    health,
    body,
    author_id,
    created_at
"""


class InitiativeRepository:
    """SQL access for `initiatives`, `initiative_projects` and
    `initiative_updates`.

    The repository receives a connection from the service layer. It never
    acquires connections, never touches the pool, and never owns a
    transaction. `asyncpg.Record` never escapes this class.

    Every statement is scoped to one workspace, and the scope arrives as a
    required keyword argument -- the shape IssueRepository establishes and
    ProjectRepository restates. `scope=` appears literally at every call site,
    so "does this query cross tenants" is answered by reading the call.

    Nothing here checks an initiative against its workspace before writing to a
    child table. Every foreign key migrations/022_initiatives.sql declares is
    composite over `workspace_id`, so PostgreSQL refuses a cross-workspace
    association as part of the statement itself. A SELECT-first check would be
    a second, weaker copy of that rule -- weaker because it is a separate
    statement the row can change between, and weaker because it would then be
    two places that have to agree.

    The one exception is `inspect_parenting`, and it is an exception because
    the rule it checks is not expressible as a constraint at all: no CHECK may
    read a second row, so a cycle among initiatives has to be found by reading
    rows. That method is only sound under `lock_parenting`; see both.
    """

    # ----------------------------------------------------------------- reads

    async def get_by_id(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        initiative_id: UUID,
    ) -> InitiativeEntity | None:
        """The initiative with this id in this workspace, or nothing.

        The workspace is part of the lookup rather than a check applied
        afterwards, so an initiative belonging to another tenant produces
        exactly the same answer as an id that exists nowhere. A caller holding
        a guessed or leaked id learns nothing by asking.
        """
        row = await connection.fetchrow(
            f"""
            SELECT {_INITIATIVE_COLUMNS}
            FROM initiatives
            WHERE initiatives.workspace_id = $1 AND initiatives.id = $2
            """,
            scope.workspace_id,
            initiative_id,
        )

        if row is None:
            return None

        return self._to_initiative(row)

    async def list(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        limit: int,
        after_created_at: datetime | None,
        after_id: UUID | None,
    ) -> Initiatives:
        """Keyset page of one workspace's initiatives, newest first.

        `limit` is expected to already be `first + 1` so the caller can detect
        a following page. No OFFSET: the cursor is a row-value comparison, and
        `workspace_id` leads both statements so a page is served by the leading
        columns of initiatives_workspace_created_at_id_idx.

        The tenant predicate is ANDed with the cursor rather than folded into
        it. Widening the row-value comparison to
        `(workspace_id, created_at, id) < (...)` would put workspaces into the
        ordering, which is how a page walk falls out of one tenant and into
        whichever one sorts next.
        """
        if after_created_at is None or after_id is None:
            rows = await connection.fetch(
                f"""
                SELECT {_INITIATIVE_COLUMNS}
                FROM initiatives
                WHERE initiatives.workspace_id = $1
                ORDER BY initiatives.created_at DESC, initiatives.id DESC
                LIMIT $2
                """,
                scope.workspace_id,
                limit,
            )
        else:
            rows = await connection.fetch(
                f"""
                SELECT {_INITIATIVE_COLUMNS}
                FROM initiatives
                WHERE initiatives.workspace_id = $1
                    AND (initiatives.created_at, initiatives.id) < ($2, $3)
                ORDER BY initiatives.created_at DESC, initiatives.id DESC
                LIMIT $4
                """,
                scope.workspace_id,
                after_created_at,
                after_id,
                limit,
            )

        return [self._to_initiative(row) for row in rows]

    async def list_updates(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        initiative_id: UUID,
        limit: int,
    ) -> Updates:
        """One initiative's update history, newest first.

        `(created_at DESC, id DESC)` and not `created_at` alone. Two updates
        posted in one transaction share a timestamp, and without the tie-break
        "which is the latest" would depend on the scan order -- which is the
        subtle way the health this history summarises comes out wrong.

        `limit` is required rather than defaulted, so the bound is a decision
        the service states out loud.
        """
        rows = await connection.fetch(
            f"""
            SELECT {_UPDATE_COLUMNS}
            FROM initiative_updates
            WHERE workspace_id = $1 AND initiative_id = $2
            ORDER BY created_at DESC, id DESC
            LIMIT $3
            """,
            scope.workspace_id,
            initiative_id,
            limit,
        )

        return [self._to_update(row) for row in rows]

    async def list_updates_for_initiatives(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        initiative_ids: Sequence[UUID],
        limit_per_initiative: int,
    ) -> Updates:
        """Every listed initiative's updates, in one statement.

        The limit is PER INITIATIVE, which is why it is a window function and
        not a `LIMIT` -- the argument ProjectRepository makes in full on
        `list_milestones_for_projects`: a plain limit over the result would
        divide one budget among however many initiatives were on the page, so
        an initiative would come back whole when read alone and truncated when
        read in a list.

        The window's ordering matches the outer ordering exactly. If the two
        disagreed the rows kept would not be the rows shown first.
        """
        rows = await connection.fetch(
            f"""
            SELECT {_UPDATE_COLUMNS}
            FROM (
                SELECT
                    {_UPDATE_COLUMNS},
                    row_number() OVER (
                        PARTITION BY initiative_id
                        ORDER BY created_at DESC, id DESC
                    ) AS rank
                FROM initiative_updates
                WHERE workspace_id = $1 AND initiative_id = ANY($2::UUID[])
            ) ranked
            WHERE rank <= $3
            ORDER BY initiative_id, created_at DESC, id DESC
            """,
            scope.workspace_id,
            list(initiative_ids),
            limit_per_initiative,
        )

        return [self._to_update(row) for row in rows]

    # ---------------------------------------------------------------- writes

    async def create(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        name: str,
        description: str | None,
        status: str,
        target_date: date | None,
        owner_id: UUID | None,
    ) -> InitiativeEntity:
        """Insert one initiative into this workspace, at the top level.

        `workspace_id` is written explicitly and carries no database default,
        so omitting it would be a NOT NULL violation rather than a quiet
        mis-filing. `status` likewise: the schema names the legal statuses and
        refuses everything else, but it does not choose one.

        No parent, deliberately. Nesting is `set_parent`, which is the only
        path that takes the lock and runs the cycle and depth checks -- an
        initiative created directly underneath a parent would be a second
        writer of `parent_initiative_id` and would silently be outside that
        guard. See the note on lock_parenting.

        No health either: health arrives by posting an update, and an
        initiative one statement old has none.

        The two aggregates come back as empty array literals rather than from
        subqueries. An initiative one statement old has no project links and no
        children -- no statement anywhere has been able to reference its id yet
        -- so the subqueries could only ever return empty.
        """
        row = await connection.fetchrow(
            """
            INSERT INTO initiatives (
                workspace_id,
                name,
                description,
                status,
                target_date,
                owner_id
            )
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING
                id,
                name,
                description,
                status,
                health,
                target_date,
                owner_id,
                parent_initiative_id,
                created_at,
                updated_at,
                ARRAY[]::UUID[] AS project_ids,
                ARRAY[]::UUID[] AS child_initiative_ids
            """,
            scope.workspace_id,
            name,
            description,
            status,
            target_date,
            owner_id,
        )

        return self._to_initiative(row)

    async def update(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        initiative_id: UUID,
        set_name: bool,
        name: str | None,
        set_description: bool,
        description: str | None,
        set_status: bool,
        status: str | None,
        set_target_date: bool,
        target_date: date | None,
        set_owner_id: bool,
        owner_id: UUID | None,
    ) -> InitiativeEntity | None:
        """Apply a partial update, or return nothing if there is no such row.

        Each field arrives as a pair -- a flag saying whether the caller
        mentioned it, and the value -- for the reason ProjectRepository.update
        gives: `COALESCE($n, column)` cannot distinguish a NULL the caller
        asked for from a field it never mentioned, and would silently treat
        every clear as a no-op.

        One static statement rather than a SET list assembled per call, and
        every value parameter cast explicitly, for that method's reasons too.

        `parent_initiative_id` and `health` are deliberately absent from the
        updatable fields. The parent is `set_parent`'s, because it needs the
        lock; the health is `post_update`'s, because changing it without
        recording who said so and why is what the update log exists to prevent.
        """
        row = await connection.fetchrow(
            f"""
            UPDATE initiatives
            SET
                name = CASE WHEN $3::BOOLEAN THEN $4::TEXT ELSE name END,
                description = CASE
                    WHEN $5::BOOLEAN THEN $6::TEXT ELSE description
                END,
                status = CASE WHEN $7::BOOLEAN THEN $8::TEXT ELSE status END,
                target_date = CASE
                    WHEN $9::BOOLEAN THEN $10::DATE ELSE target_date
                END,
                owner_id = CASE
                    WHEN $11::BOOLEAN THEN $12::UUID ELSE owner_id
                END,
                updated_at = now()
            WHERE initiatives.workspace_id = $1 AND initiatives.id = $2
            RETURNING {_INITIATIVE_COLUMNS}
            """,
            scope.workspace_id,
            initiative_id,
            set_name,
            name,
            set_description,
            description,
            set_status,
            status,
            set_target_date,
            target_date,
            set_owner_id,
            owner_id,
        )

        if row is None:
            return None

        return self._to_initiative(row)

    async def set_health(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        initiative_id: UUID,
        health: str,
    ) -> None:
        """Stamp the current health onto the initiative row.

        Called by the service in the same transaction as the update row that
        reported it, and never on its own -- see the long note on
        `projects.health` in migrations/022_initiatives.sql for why both the
        column and the log exist, and what keeps them in step.

        Reports nothing. A row that does not match is not a case a caller can
        act on: the insert of the update row has already run in this
        transaction and its foreign key would have refused an initiative that
        is not in this workspace.
        """
        await connection.execute(
            """
            UPDATE initiatives
            SET health = $3, updated_at = now()
            WHERE workspace_id = $1 AND id = $2
            """,
            scope.workspace_id,
            initiative_id,
            health,
        )

    async def delete(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        initiative_id: UUID,
    ) -> bool:
        """Delete the initiative, reporting whether there was one to delete.

        The workspace is in the predicate, so this cannot reach another
        tenant's initiative however the id was obtained.

        This does NOT remove the rows that reference it: every foreign key onto
        `initiatives` is ON DELETE RESTRICT, so an initiative still carrying
        project links, updates or children makes the server refuse this
        statement. InitiativeService.delete clears them first, in one
        transaction.
        """
        # Annotated rather than compared inline: asyncpg ships no types, so
        # `execute` is Any and `Any == str` would silently satisfy `bool`.
        status: str = await connection.execute(
            """
            DELETE FROM initiatives
            WHERE workspace_id = $1 AND id = $2
            """,
            scope.workspace_id,
            initiative_id,
        )

        return status == "DELETE 1"

    # -------------------------------------------------------- the hierarchy

    async def lock_parenting(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
    ) -> None:
        """Serialise initiative re-parenting within one workspace.

        This is the whole of the cycle guard's soundness, so it is worth being
        precise about what it does -- the argument is
        `RelationRepository.lock_parenting`'s, applied to a second graph.

        `inspect_parenting` reads a hierarchy and `set_parent` changes one;
        between the two, another transaction re-parenting a different
        initiative could invalidate what the first one read, and the two writes
        together would form a cycle neither could see on its own. A lock held
        across both closes that, and an advisory lock is what this needs rather
        than row locks: the rows that would have to be locked are the ones the
        walk has not reached yet.

        Per workspace, not global, so tenants do not queue behind each other;
        and `_xact_`, so the caller's commit or rollback releases it with no
        cleanup path that could be skipped. `hashtext` collisions between two
        workspaces cost a little extra serialisation and cannot cost
        correctness.

        This is NOT a substitute for a database constraint and does not pretend
        to be one. It binds only callers that take it. Any future code that
        writes `initiatives.parent_initiative_id` -- a bulk import, an
        `initiativeCreate` that accepts a parent, an operator's UPDATE -- can
        still write a cycle, and nothing here will notice.
        """
        await connection.execute(
            "SELECT pg_advisory_xact_lock($1, hashtext($2))",
            INITIATIVE_PARENTING_LOCK_CLASS,
            str(scope.workspace_id),
        )

    async def inspect_parenting(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        parent_id: UUID,
        initiative_id: UUID,
        max_depth: int,
    ) -> ParentingCheck:
        """Everything a proposed re-parent has to be judged on, in one read.

        Two walks in one statement: up from the proposed PARENT, and down from
        the initiative being MOVED. Between them they answer both refusals --
        a cycle, and a tree that would grow past `max_depth` -- so a caller
        pays one round trip rather than two, inside a lock it is holding.

        Walking up from the parent rather than down from the child is what
        keeps the cycle check proportional to tree DEPTH instead of to
        sub-tree size; the downward walk is bounded by `max_depth` for the same
        reason, and is what makes the depth rule apply to the whole sub-tree
        being moved rather than only to its root.

        `depth < $4` on both recursive terms is the bound the file at the top
        of migrations/022_initiatives.sql promises. It is what stops this being
        an unbounded recursion over a table a client can grow: whatever a
        workspace holds, this reads at most `max_depth` rows in each direction
        per branch. Truncation is safe in one direction only, and deliberately
        so -- a truncated count can only be at or above the limit, so it
        produces refusals that are correct and never approvals that are not.
        A cycle far enough away to fall outside the upward walk necessarily
        makes `parent_depth` reach the limit, so the depth refusal catches it.

        `CYCLE id SET ... USING ...` on both is not decoration. A recursive
        term over a hierarchy that already contained a cycle would not return a
        wrong answer, it would never return at all -- and the one moment this
        query runs is the moment someone is trying to create one. The clause
        makes PostgreSQL stop at the first repeated id, so a database whose
        invariant has already been broken by some other writer produces an
        answer, and (because the repeated id is still emitted once) an answer
        that still refuses the write.

        The workspace predicate is on the recursive terms as well as on the
        anchors. Without it a walk would leave the tenant the moment it touched
        a row whose parent was written before this migration's foreign key
        existed.
        """
        row = await connection.fetchrow(
            """
            WITH RECURSIVE ancestors (id, parent_initiative_id, depth) AS (
                SELECT id, parent_initiative_id, 0
                FROM initiatives
                WHERE workspace_id = $1 AND id = $2

                UNION ALL

                SELECT above.id, above.parent_initiative_id, ancestors.depth + 1
                FROM initiatives above
                JOIN ancestors ON above.id = ancestors.parent_initiative_id
                WHERE above.workspace_id = $1 AND ancestors.depth < $4
            ) CYCLE id SET ancestor_cycle USING ancestor_path,
            descendants (id, depth) AS (
                SELECT id, 0
                FROM initiatives
                WHERE workspace_id = $1 AND id = $3

                UNION ALL

                SELECT below.id, descendants.depth + 1
                FROM initiatives below
                JOIN descendants ON below.parent_initiative_id = descendants.id
                WHERE below.workspace_id = $1 AND descendants.depth < $4
            ) CYCLE id SET descendant_cycle USING descendant_path
            SELECT
                EXISTS (
                    SELECT 1 FROM ancestors WHERE ancestors.id = $3
                ) AS creates_cycle,
                COALESCE((SELECT max(depth) FROM ancestors), 0) AS parent_depth,
                COALESCE((SELECT max(depth) FROM descendants), 0) AS subtree_height
            """,
            scope.workspace_id,
            parent_id,
            initiative_id,
            max_depth,
        )

        return ParentingCheck(
            creates_cycle=row["creates_cycle"],
            parent_depth=row["parent_depth"],
            subtree_height=row["subtree_height"],
        )

    async def set_parent(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        initiative_id: UUID,
        parent_id: UUID | None,
    ) -> InitiativeEntity | None:
        """Attach or detach a parent; None if the initiative is not here.

        The parent is not checked before the write. `initiatives_parent_fk` is
        a composite key onto `initiatives (workspace_id, id)`, so a parent in
        another workspace has no matching row and the server refuses this
        statement -- and a SELECT here first would be a second, weaker copy of
        that rule, weaker because the parent could be deleted between the two
        statements.

        `parent_id` of None is the detach, which needs no guard: removing an
        edge cannot close a loop and cannot deepen a tree.
        """
        row = await connection.fetchrow(
            f"""
            UPDATE initiatives
            SET parent_initiative_id = $3, updated_at = now()
            WHERE initiatives.workspace_id = $1 AND initiatives.id = $2
            RETURNING {_INITIATIVE_COLUMNS}
            """,
            scope.workspace_id,
            initiative_id,
            parent_id,
        )

        if row is None:
            return None

        return self._to_initiative(row)

    async def clear_children(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        initiative_id: UUID,
    ) -> None:
        """Detach every child of one initiative, on the way to deleting it.

        The children are promoted to the top level rather than deleted, which
        is the product decision `initiatives_parent_fk`'s RESTRICT forces
        somebody to make out loud: deleting a goal must not silently destroy
        the goals beneath it.
        """
        await connection.execute(
            """
            UPDATE initiatives
            SET parent_initiative_id = NULL, updated_at = now()
            WHERE workspace_id = $1 AND parent_initiative_id = $2
            """,
            scope.workspace_id,
            initiative_id,
        )

    # ---------------------------------------------------------- project links

    async def add_project(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        initiative_id: UUID,
        project_id: UUID,
    ) -> None:
        """Associate one project with one initiative.

        The single `$1` feeding both foreign keys is the whole mechanism: the
        row carries one workspace, so the initiative and the project are
        checked against the same tenant. A project from another workspace
        raises ForeignKeyViolationError on
        `initiative_projects_project_fk`, an initiative from another workspace
        raises it on `initiative_projects_initiative_fk`, and a duplicate
        raises UniqueViolationError on `initiative_projects_pkey`. All three
        are expected outcomes of client input; InitiativeService names them by
        constraint and translates them, and translates nothing else.

        No `ON CONFLICT DO NOTHING`. It would make this idempotent by hiding
        the one answer the caller might act on -- whether the association was
        already there -- and this repository has no way to report back what it
        chose to ignore.
        """
        await connection.execute(
            """
            INSERT INTO initiative_projects (
                workspace_id, initiative_id, project_id
            )
            VALUES ($1, $2, $3)
            """,
            scope.workspace_id,
            initiative_id,
            project_id,
        )

    async def remove_project(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        initiative_id: UUID,
        project_id: UUID,
    ) -> bool:
        """Dissociate one project, reporting whether it was associated.

        An initiative from another workspace matches nothing here, so the
        answer is the same `False` an unassociated project produces -- the same
        indistinguishability every read in this class provides, on the write
        path.
        """
        # Annotated rather than compared inline: asyncpg ships no types, so
        # `execute` is Any and `Any == str` would silently satisfy `bool`.
        status: str = await connection.execute(
            """
            DELETE FROM initiative_projects
            WHERE workspace_id = $1 AND initiative_id = $2 AND project_id = $3
            """,
            scope.workspace_id,
            initiative_id,
            project_id,
        )

        return status == "DELETE 1"

    async def clear_projects(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        initiative_id: UUID,
    ) -> None:
        """Drop every project link of one initiative, before deleting it."""
        await connection.execute(
            """
            DELETE FROM initiative_projects
            WHERE workspace_id = $1 AND initiative_id = $2
            """,
            scope.workspace_id,
            initiative_id,
        )

    async def clear_project_links(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        project_id: UUID,
    ) -> None:
        """Drop every initiative link of one PROJECT, before deleting it.

        The mirror of `clear_projects`, and it lives here rather than on
        ProjectRepository because SQL against `initiative_projects` belongs to
        the repository that owns that table. ProjectService reaches across to
        this method inside its own delete transaction -- the same shape it
        already uses for IssueRepository.
        """
        await connection.execute(
            """
            DELETE FROM initiative_projects
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
        initiative_id: UUID,
        health: str,
        body: str,
        author_id: UUID,
    ) -> InitiativeUpdateEntity:
        """Append one update to an initiative's history.

        Neither the initiative nor the author is read first.
        `initiative_updates_initiative_fk` and `initiative_updates_author_fk`
        are both composite over the same `workspace_id`, so an initiative from
        another tenant and an author who is not a member of this one are both
        refused by the statement that would have written the row -- with no
        window between a check and a write for a membership to be revoked in.
        """
        row = await connection.fetchrow(
            f"""
            INSERT INTO initiative_updates (
                workspace_id, initiative_id, health, body, author_id
            )
            VALUES ($1, $2, $3, $4, $5)
            RETURNING {_UPDATE_COLUMNS}
            """,
            scope.workspace_id,
            initiative_id,
            health,
            body,
            author_id,
        )

        return self._to_update(row)

    async def clear_updates(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        initiative_id: UUID,
    ) -> None:
        """Drop one initiative's whole history, on the way to deleting it."""
        await connection.execute(
            """
            DELETE FROM initiative_updates
            WHERE workspace_id = $1 AND initiative_id = $2
            """,
            scope.workspace_id,
            initiative_id,
        )

    # --------------------------------------------------------------- mapping

    @staticmethod
    def _to_initiative(row: asyncpg.Record) -> InitiativeEntity:
        # The two id arrays arrive as Python lists; the entity is frozen, so
        # they are copied into tuples rather than handed out as mutable
        # aliases of whatever asyncpg built.
        return InitiativeEntity(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            status=row["status"],
            health=row["health"],
            target_date=row["target_date"],
            owner_id=row["owner_id"],
            parent_initiative_id=row["parent_initiative_id"],
            project_ids=tuple(row["project_ids"]),
            child_initiative_ids=tuple(row["child_initiative_ids"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _to_update(row: asyncpg.Record) -> InitiativeUpdateEntity:
        return InitiativeUpdateEntity(
            id=row["id"],
            initiative_id=row["initiative_id"],
            health=row["health"],
            body=row["body"],
            author_id=row["author_id"],
            created_at=row["created_at"],
        )
