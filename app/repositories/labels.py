from uuid import UUID

import asyncpg

from app.domain.labels import LabelEntity
from app.domain.tenancy import WorkspaceScope


class LabelRepository:
    """SQL access for `labels`.

    The repository receives a connection from the service layer. It never
    acquires connections, never touches the pool, and never owns a
    transaction. `asyncpg.Record` never escapes this class.

    Every statement is scoped to one workspace and the scope arrives as a
    required keyword argument, for the reasons `IssueRepository` spells out:
    a caller cannot reach this class without having decided which tenant it
    addresses, and `scope=` appears literally at every call site, so "does
    this query cross tenants" is answered by reading the call.

    Column lists are written out in every statement rather than assembled
    from a shared constant. Interpolating any part of a statement -- even a
    constant nobody can influence -- makes "is every query here built without
    string formatting" a question a reader has to answer per call site
    instead of once.

    Nothing here translates a constraint violation. `labels_workspace_name_key`
    is a duplicate name and is a client's to correct; `labels_name_length` and
    `labels_color_format` mean the service and the schema disagree, which is a
    defect. Telling those apart is a business rule, so it lives in the service
    and this class lets asyncpg raise.
    """

    async def get_by_id(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        label_id: UUID,
    ) -> LabelEntity | None:
        """The label with this id in this workspace, or nothing.

        The workspace is part of the lookup rather than a check applied
        afterwards, so a label belonging to another tenant answers exactly as
        an id that exists nowhere. See `IssueRepository.get_by_id` for why
        that equivalence is a security property and not merely tidy.
        """
        row = await connection.fetchrow(
            """
            SELECT
                id,
                name,
                color,
                created_at,
                updated_at
            FROM labels
            WHERE workspace_id = $1 AND id = $2
            """,
            scope.workspace_id,
            label_id,
        )

        if row is None:
            return None

        return self._to_entity(row)

    async def create(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        name: str,
        color: str,
    ) -> LabelEntity:
        """Insert one label into this workspace.

        `workspace_id` is written explicitly and carries no database default,
        so a statement that forgot the tenant is a NOT NULL violation rather
        than a row filed under whichever workspace a default named.
        """
        row = await connection.fetchrow(
            """
            INSERT INTO labels (workspace_id, name, color)
            VALUES ($1, $2, $3)
            RETURNING
                id,
                name,
                color,
                created_at,
                updated_at
            """,
            scope.workspace_id,
            name,
            color,
        )

        return self._to_entity(row)

    async def update(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        label_id: UUID,
        name: str,
        color: str,
    ) -> LabelEntity | None:
        """Replace this label's editable fields, or answer nothing.

        The workspace is in the WHERE clause, so an id belonging to another
        tenant updates no rows and returns None -- the same answer as an id
        that exists nowhere, reached without ever reading the other tenant's
        row. A read-then-write would have to fetch it first in order to
        discover it was not ours.

        `updated_at` is assigned in the statement because this schema has no
        touch trigger; see the head of migrations/007_labels_comments.sql for
        why it has none. Assigning it here rather than from Python keeps the
        value on the server's clock, which is the clock `created_at` came
        from.
        """
        row = await connection.fetchrow(
            """
            UPDATE labels
            SET name = $3,
                color = $4,
                updated_at = now()
            WHERE workspace_id = $1 AND id = $2
            RETURNING
                id,
                name,
                color,
                created_at,
                updated_at
            """,
            scope.workspace_id,
            label_id,
            name,
            color,
        )

        if row is None:
            return None

        return self._to_entity(row)

    async def delete(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        label_id: UUID,
    ) -> bool:
        """Remove this label from this workspace; True if a row went.

        Every `issue_labels` row wearing it goes too, through
        `issue_labels_label_fk ON DELETE CASCADE`. That is what the cascade is
        for -- deleting a label is how you stop using it -- and it destroys no
        issue, only the associations.

        RETURNING rather than the command tag: a tag has to be parsed out of a
        string, and a boolean is what the caller actually asked for.
        """
        deleted = await connection.fetchval(
            """
            DELETE FROM labels
            WHERE workspace_id = $1 AND id = $2
            RETURNING id
            """,
            scope.workspace_id,
            label_id,
        )

        return deleted is not None

    async def list(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        limit: int,
        after_name: str | None,
        after_id: UUID | None,
    ) -> list[LabelEntity]:
        """Keyset page of one workspace's labels, alphabetically.

        `limit` is expected to already be `first + 1` so the caller can detect
        a following page. No OFFSET: the cursor is a row-value comparison, and
        `workspace_id` leads both statements so a page is served by the
        leading columns of `labels_workspace_name_idx`.

        The tenant predicate is ANDed with the cursor rather than folded into
        it, exactly as in `IssueRepository.list`. Widening the row-value
        comparison to `(workspace_id, name, id) > (...)` would put workspaces
        into the ordering, which is how a page walk falls out of one tenant
        and into whichever one sorts next.

        Ascending, because a label list is a picker and a picker reads
        alphabetically. The ordering is the database's collation applied to
        the stored name, and the cursor carries that name back unmodified, so
        the comparison and the ordering cannot disagree about it.
        """
        if after_name is None or after_id is None:
            rows = await connection.fetch(
                """
                SELECT
                    id,
                    name,
                    color,
                    created_at,
                    updated_at
                FROM labels
                WHERE workspace_id = $1
                ORDER BY name, id
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
                    color,
                    created_at,
                    updated_at
                FROM labels
                WHERE workspace_id = $1 AND (name, id) > ($2, $3)
                ORDER BY name, id
                LIMIT $4
                """,
                scope.workspace_id,
                after_name,
                after_id,
                limit,
            )

        return [self._to_entity(row) for row in rows]

    @staticmethod
    def _to_entity(row: asyncpg.Record) -> LabelEntity:
        return LabelEntity(
            id=row["id"],
            name=row["name"],
            color=row["color"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
