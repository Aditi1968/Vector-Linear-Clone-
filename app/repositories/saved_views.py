import json
from uuid import UUID

import asyncpg

from app.domain.issues import IssueFilter, IssueOrder, IssueOrderField, OrderDirection
from app.domain.saved_views import (
    FavoriteEntity,
    SavedViewEntity,
    decode_filter,
    encode_filter,
)
from app.domain.tenancy import WorkspaceScope


# The columns a SavedViewEntity is built from, as one expression list shared by
# every statement below.
#
# Interpolated with an f-string, which in this repository needs the defence
# `app/repositories/issues.py` gives for ISSUE_COLUMNS: "parameterized SQL
# only" is a rule about VALUES, and this is a module-level literal no input can
# influence. Five copies of it is how a column gets added to four statements
# and forgotten in the fifth, which surfaces as a KeyError from `_to_view` on
# whichever path happened not to be exercised.
SAVED_VIEW_COLUMNS = """
                id,
                team_id,
                name,
                filter,
                order_field,
                order_direction,
                layout,
                grouping,
                subgrouping,
                visibility,
                created_by,
                created_at,
                updated_at
"""

# What makes a view readable: it is shared, or the viewer wrote it.
#
# One string used by every read, rather than the same disjunction typed into
# five statements. This is the predicate that decides whether one member sees
# another's private list, so the copies would be five chances to leave one of
# them out -- and the statement that lost it would still return rows, just more
# of them than it should.
#
# `$1` is always the workspace and `$2` always the viewer, in every statement
# that interpolates this.
_VISIBLE_TO_VIEWER = "(visibility = 'shared' OR created_by = $2)"


class SavedViewRepository:
    """SQL access for `saved_views`.

    The repository receives a connection from the service layer. It never
    acquires connections, never touches the pool, and never owns a
    transaction. `asyncpg.Record` never escapes this class.

    Every statement is scoped to one workspace AND to one viewer, and both
    arrive as required keyword arguments. The workspace is the tenancy
    predicate every repository here carries; the viewer is the second half,
    because a personal view belongs to a person and not merely to a tenant.
    Neither is optional and neither has a default, so a caller cannot reach
    this class without having decided whose data it is reading -- and
    `viewer_id=` appears literally at every call site, so "could this return
    somebody else's private view" is answered by reading the call.

    The `filter` column is JSON authored by whoever created the view, and it
    is never handed on as JSON. `_to_view` runs it through
    `app.domain.saved_views.decode_filter`, so everything leaving this class
    is a typed `IssueFilter` -- which is what lets `IssueRepository` bind it
    as parameters exactly as it binds a filter that arrived on the wire.
    """

    # ----------------------------------------------------------------- reads

    async def get_by_id(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        viewer_id: UUID,
        saved_view_id: UUID,
    ) -> SavedViewEntity | None:
        """The view with this id, if this viewer may read it.

        Three situations produce the same nothing: no such view anywhere, a
        view in another workspace, and somebody else's personal view. That is
        deliberate -- distinguishing the last one would answer "does this
        person have a private view with this id", which is a question about
        another member's workspace behaviour that no client may ask.
        """
        row = await connection.fetchrow(
            f"""
            SELECT
                {SAVED_VIEW_COLUMNS}
            FROM saved_views
            WHERE workspace_id = $1 AND id = $3 AND {_VISIBLE_TO_VIEWER}
            """,
            scope.workspace_id,
            viewer_id,
            saved_view_id,
        )

        if row is None:
            return None

        return self._to_view(row)

    async def list(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        viewer_id: UUID,
        filter_by_team: bool,
        team_id: UUID | None,
        limit: int,
        after_name: str | None,
        after_id: UUID | None,
    ) -> list[SavedViewEntity]:
        """Keyset page of the views this viewer may read, alphabetically.

        `limit` is expected to already be `first + 1` so the caller can detect
        a following page. No OFFSET: the cursor is a row-value comparison over
        `(name, id)`, and `workspace_id` leads the statement either way, so a
        page is served by the leading columns of
        saved_views_workspace_name_id_idx.

        The tenant predicate is ANDed with the cursor rather than folded into
        it. Widening the comparison to `(workspace_id, name, id) > (...)`
        would put workspaces into the ordering, which is how a page walk falls
        out of one tenant and into whichever one sorts next.

        The team narrowing is TWO arguments, and it has to be. "The views for
        team ENG" and "the views that belong to no team" are different
        requests, and a single nullable `team_id` cannot tell the second from
        "I am not narrowing on team at all" -- the same three-state problem
        `IssueFilter` solves with UNSET. `filter_by_team` is whether to
        narrow; `team_id` is what to narrow to, and `IS NOT DISTINCT FROM` is
        the one comparison that answers a NULL target, since `= NULL` is never
        true.

        `$5::TEXT IS NULL` guards the cursor half rather than the statement
        text branching on it. `name` is NOT NULL and non-empty -- 019's
        saved_views_name_length says so -- so a NULL there is unambiguously
        "no cursor" and cannot collide with a real position. One statement
        also means one prepared plan for a table whose whole content is a
        workspace's few dozen views.
        """
        rows = await connection.fetch(
            f"""
            SELECT
                {SAVED_VIEW_COLUMNS}
            FROM saved_views
            WHERE workspace_id = $1
                AND {_VISIBLE_TO_VIEWER}
                AND ($3::BOOLEAN IS FALSE OR team_id IS NOT DISTINCT FROM $4::UUID)
                AND ($5::TEXT IS NULL OR (name, id) > ($5::TEXT, $6::UUID))
            ORDER BY name, id
            LIMIT $7
            """,
            scope.workspace_id,
            viewer_id,
            filter_by_team,
            team_id,
            after_name,
            after_id,
            limit,
        )

        return [self._to_view(row) for row in rows]

    # ---------------------------------------------------------------- writes

    async def create(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        creator_id: UUID,
        team_id: UUID | None,
        name: str,
        issue_filter: IssueFilter,
        order: IssueOrder,
        layout: str,
        grouping: str | None,
        subgrouping: str | None,
        visibility: str,
    ) -> SavedViewEntity:
        """Insert one saved view into this workspace.

        `workspace_id` and `created_by` are written into the SAME row, which
        is what makes `saved_views_creator_fk` a check worth having: both
        halves of that composite key come from this one statement, so an
        author who is not a member of THIS workspace raises
        ForeignKeyViolationError here rather than being admitted by a SELECT
        the caller ran a moment earlier. There is no pre-check, and the same
        goes for `team_id` against `saved_views_team_fk`.

        The filter is serialised with `json.dumps` and cast, rather than
        passed as a dict. asyncpg encodes JSONB from `str` or `bytes` only --
        a dict raises DataError -- and the explicit `::JSONB` is what stops
        the parameter being inferred as TEXT beside a JSONB column.
        """
        row = await connection.fetchrow(
            f"""
            INSERT INTO saved_views (
                workspace_id,
                created_by,
                team_id,
                name,
                filter,
                order_field,
                order_direction,
                layout,
                grouping,
                subgrouping,
                visibility
            )
            VALUES ($1, $2, $3, $4, $5::JSONB, $6, $7, $8, $9, $10, $11)
            RETURNING
                {SAVED_VIEW_COLUMNS}
            """,
            scope.workspace_id,
            creator_id,
            team_id,
            name,
            json.dumps(encode_filter(issue_filter)),
            order.field.value,
            order.direction.value,
            layout,
            grouping,
            subgrouping,
            visibility,
        )

        return self._to_view(row)

    async def update(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        viewer_id: UUID,
        saved_view_id: UUID,
        set_name: bool,
        name: str | None,
        set_team_id: bool,
        team_id: UUID | None,
        set_filter: bool,
        issue_filter: IssueFilter | None,
        set_order: bool,
        order: IssueOrder | None,
        set_layout: bool,
        layout: str | None,
        set_grouping: bool,
        grouping: str | None,
        set_subgrouping: bool,
        subgrouping: str | None,
        set_visibility: bool,
        visibility: str | None,
    ) -> SavedViewEntity | None:
        """Apply a partial update, or return nothing if there is no such row.

        `created_by = $2` in the WHERE and not merely the visibility
        predicate: reading a shared view is something every member may do,
        and rewriting one is not. A member who edits somebody else's shared
        view gets the same None a nonexistent id gets, which is the same
        answer they would get for a view in another workspace -- so the write
        path leaks no more than the read path does.

        Each field arrives as a pair: a flag saying whether the caller
        mentioned it, and the value. That is what makes "clear the grouping"
        expressible at all -- `COALESCE($n, grouping)` cannot distinguish a
        NULL the caller asked for from a field it never mentioned, and would
        silently treat every clear as a no-op.

        One static statement rather than a SET list assembled per call, and
        every value parameter cast explicitly, for the reasons
        `ProjectRepository.update` states: the SQL text is the same for every
        combination of fields, so there is no place for a column name to
        arrive from anywhere but this file, and a NULL parameter in an
        untyped CASE branch is exactly where type inference gets reported back
        as "could not determine data type".

        Setting `team_id` re-checks `saved_views_team_fk` against the row's
        existing `workspace_id`, which this statement never touches -- so a
        team from another tenant is refused here exactly as it is on insert.
        """
        row = await connection.fetchrow(
            f"""
            UPDATE saved_views
            SET
                name = CASE WHEN $4::BOOLEAN THEN $5::TEXT ELSE name END,
                team_id = CASE WHEN $6::BOOLEAN THEN $7::UUID ELSE team_id END,
                filter = CASE WHEN $8::BOOLEAN THEN $9::JSONB ELSE filter END,
                order_field = CASE
                    WHEN $10::BOOLEAN THEN $11::TEXT ELSE order_field
                END,
                order_direction = CASE
                    WHEN $10::BOOLEAN THEN $12::TEXT ELSE order_direction
                END,
                layout = CASE WHEN $13::BOOLEAN THEN $14::TEXT ELSE layout END,
                grouping = CASE WHEN $15::BOOLEAN THEN $16::TEXT ELSE grouping END,
                subgrouping = CASE
                    WHEN $17::BOOLEAN THEN $18::TEXT ELSE subgrouping
                END,
                visibility = CASE
                    WHEN $19::BOOLEAN THEN $20::TEXT ELSE visibility
                END,
                updated_at = now()
            WHERE workspace_id = $1 AND created_by = $2 AND id = $3
            RETURNING
                {SAVED_VIEW_COLUMNS}
            """,
            scope.workspace_id,
            viewer_id,
            saved_view_id,
            set_name,
            name,
            set_team_id,
            team_id,
            set_filter,
            None if issue_filter is None else json.dumps(encode_filter(issue_filter)),
            set_order,
            None if order is None else order.field.value,
            None if order is None else order.direction.value,
            set_layout,
            layout,
            set_grouping,
            grouping,
            set_subgrouping,
            subgrouping,
            set_visibility,
            visibility,
        )

        if row is None:
            return None

        return self._to_view(row)

    async def delete(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        viewer_id: UUID,
        saved_view_id: UUID,
    ) -> bool:
        """Delete the view, reporting whether there was one to delete.

        Scoped to the creator for the reason `update` gives. This does NOT
        remove the favourites pointing at the view: `favorites_saved_view_fk`
        is ON DELETE RESTRICT, so a view somebody has favourited makes the
        server refuse this statement. SavedViewService.delete clears them
        first, in one transaction -- see the note there on why the schema
        refuses to do it silently.
        """
        # Annotated rather than compared inline: asyncpg ships no types, so
        # `execute` is Any and `Any == str` would silently satisfy `bool`.
        status: str = await connection.execute(
            """
            DELETE FROM saved_views
            WHERE workspace_id = $1 AND created_by = $2 AND id = $3
            """,
            scope.workspace_id,
            viewer_id,
            saved_view_id,
        )

        return status == "DELETE 1"

    # ---------------------------------------------------------------- mapping

    @staticmethod
    def _to_view(row: asyncpg.Record) -> SavedViewEntity:
        """One row as an entity, with the stored filter parsed on the way.

        `decode_filter` is called HERE rather than left to a caller, so there
        is no shape of this feature in which a raw stored document is reachable
        above the repository. It raises InvalidStoredFilterError on a document
        this application did not write, which is a defect rather than bad
        input; see that exception for why it must not be softened into a
        widened filter.

        `filter` comes back as `str` and not as a dict: asyncpg decodes JSONB
        to text unless a codec is registered, and registering one here would
        make this mapping depend on connection setup performed somewhere else.

        `IssueOrderField(...)` and `OrderDirection(...)` raise ValueError on a
        row holding a value the enum does not know -- the right failure, since
        the only way to store one is a migration that widened
        saved_views_order_field_check without widening the enum.
        """
        return SavedViewEntity(
            id=row["id"],
            team_id=row["team_id"],
            name=row["name"],
            issue_filter=decode_filter(json.loads(row["filter"])),
            order=IssueOrder(
                field=IssueOrderField(row["order_field"]),
                direction=OrderDirection(row["order_direction"]),
            ),
            layout=row["layout"],
            grouping=row["grouping"],
            subgrouping=row["subgrouping"],
            visibility=row["visibility"],
            created_by=row["created_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


class FavoriteRepository:
    """SQL access for `favorites`.

    Receives its connection, owns no transaction, and lets no
    `asyncpg.Record` escape, like every repository here.

    Every statement is keyed on the PAIR (workspace, user). A favourite is
    per-user and per-workspace, so neither half alone identifies a list: one
    person has a separate sidebar in each workspace they belong to, and one
    workspace's `favorites` rows belong to many different people.
    """

    async def list(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        user_id: UUID,
        limit: int,
    ) -> list[FavoriteEntity]:
        """One person's favourites in this workspace, in their own order.

        Not paginated, and that is a product statement rather than an
        oversight: a sidebar is a handful of rows a client renders whole, so a
        cursor would buy a round trip and a `hasNextPage` nobody reads. The
        same reasoning `ProjectRepository.list_milestones` records.

        `ORDER BY position, id` -- `id` because `position` is not unique, so
        without it two favourites sharing a number come back in whatever order
        the scan produced, differently between calls and between replicas. It
        is also what makes the truncation deterministic.
        """
        rows = await connection.fetch(
            """
            SELECT id, team_id, project_id, saved_view_id, position, created_at
            FROM favorites
            WHERE workspace_id = $1 AND user_id = $2
            ORDER BY position, id
            LIMIT $3
            """,
            scope.workspace_id,
            user_id,
            limit,
        )

        return [self._to_favorite(row) for row in rows]

    async def add(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        user_id: UUID,
        team_id: UUID | None,
        project_id: UUID | None,
        saved_view_id: UUID | None,
    ) -> FavoriteEntity | None:
        """Append one favourite to the end of this person's list.

        Returns None when the target is a saved view this person may not read
        -- somebody else's personal one -- which is the one refusal the
        schema cannot make. `favorites_saved_view_fk` checks the tenant and
        stops there; visibility is a comparison between two tables' columns,
        which is a JOIN and not a constraint. So the guard is a `WHERE EXISTS`
        in this same statement, which is what closes the window a
        SELECT-then-INSERT would leave for the view to be made private in.

        A team or a project needs no such guard: everything in the workspace
        is visible to its members, and the composite foreign keys refuse a
        target from another tenant as part of the insert.

        The position is computed by the server inside the same statement, as
        `MAX(position) + 1` over this person's existing favourites. A
        SELECT-then-INSERT would leave a window in which another request
        appends and both writes land on the same number. This narrows that
        window rather than closing it -- under READ COMMITTED two concurrent
        appends can still read the same maximum -- which is survivable BY
        DESIGN and not by luck: `position` carries no UNIQUE constraint and
        every ordering breaks ties with `id`, so two favourites sharing a
        position are ordered stably rather than ambiguously.

        A duplicate raises UniqueViolationError on whichever of the three
        `favorites_*_key` constraints matches the target, and the service
        names them. No `ON CONFLICT DO NOTHING`: it would make this idempotent
        by hiding the one answer the caller might act on.
        """
        row = await connection.fetchrow(
            """
            INSERT INTO favorites (
                workspace_id,
                user_id,
                team_id,
                project_id,
                saved_view_id,
                position
            )
            SELECT
                $1,
                $2,
                $3,
                $4,
                $5,
                COALESCE(
                    (
                        SELECT MAX(existing.position) + 1
                        FROM favorites existing
                        WHERE existing.workspace_id = $1 AND existing.user_id = $2
                    ),
                    0
                )
            WHERE $5::UUID IS NULL
                OR EXISTS (
                    SELECT 1
                    FROM saved_views
                    WHERE saved_views.workspace_id = $1
                        AND saved_views.id = $5
                        AND (
                            saved_views.visibility = 'shared'
                            OR saved_views.created_by = $2
                        )
                )
            RETURNING id, team_id, project_id, saved_view_id, position, created_at
            """,
            scope.workspace_id,
            user_id,
            team_id,
            project_id,
            saved_view_id,
        )

        if row is None:
            return None

        return self._to_favorite(row)

    async def remove(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        user_id: UUID,
        favorite_id: UUID,
    ) -> bool:
        """Drop one favourite, reporting whether there was one to drop.

        The user is in the predicate beside the workspace, so this cannot
        reach another member's list however the id was obtained -- and a
        favourite belonging to somebody else produces the same `False` an id
        that exists nowhere does.
        """
        # Annotated rather than compared inline: asyncpg ships no types, so
        # `execute` is Any and `Any == str` would silently satisfy `bool`.
        status: str = await connection.execute(
            """
            DELETE FROM favorites
            WHERE workspace_id = $1 AND user_id = $2 AND id = $3
            """,
            scope.workspace_id,
            user_id,
            favorite_id,
        )

        return status == "DELETE 1"

    async def set_position(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        user_id: UUID,
        favorite_id: UUID,
        position: int,
    ) -> FavoriteEntity | None:
        """Move one favourite in this person's list, or report no such row.

        Only the named row moves; nothing else is renumbered. Positions are
        not required to be unique or contiguous -- see the note on the column
        in migration 019 -- so a client that drags an item between two others
        may send any number between them, and a client that has run out of
        room renumbers the list by calling this once per item.
        """
        row = await connection.fetchrow(
            """
            UPDATE favorites
            SET position = $4
            WHERE workspace_id = $1 AND user_id = $2 AND id = $3
            RETURNING id, team_id, project_id, saved_view_id, position, created_at
            """,
            scope.workspace_id,
            user_id,
            favorite_id,
            position,
        )

        if row is None:
            return None

        return self._to_favorite(row)

    async def clear_for_saved_view(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        saved_view_id: UUID,
    ) -> None:
        """Drop every favourite pointing at one view, whoever owns it.

        Only used on the way to deleting the view, which is the one operation
        that legitimately reaches into other members' lists: the thing their
        shortcut pointed at is about to stop existing. Not scoped by user for
        exactly that reason, and scoped by workspace like everything else.

        It reports no count, because there is nothing a caller could do
        differently for zero.
        """
        await connection.execute(
            """
            DELETE FROM favorites
            WHERE workspace_id = $1 AND saved_view_id = $2
            """,
            scope.workspace_id,
            saved_view_id,
        )

    @staticmethod
    def _to_favorite(row: asyncpg.Record) -> FavoriteEntity:
        return FavoriteEntity(
            id=row["id"],
            team_id=row["team_id"],
            project_id=row["project_id"],
            saved_view_id=row["saved_view_id"],
            position=row["position"],
            created_at=row["created_at"],
        )
