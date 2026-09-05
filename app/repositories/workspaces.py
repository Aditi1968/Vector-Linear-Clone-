from uuid import UUID

import asyncpg


class WorkspaceRepository:
    """SQL access for `workspaces`.

    The repository receives a connection from the service layer. It never
    acquires connections, never touches the pool, and never owns a
    transaction. `asyncpg.Record` never escapes this class.
    """

    async def find_id_by_slug(
        self,
        connection: asyncpg.Connection,
        slug: str,
    ) -> UUID | None:
        """Resolve a slug to its workspace id, or nothing.

        The comparison is exact and the slug is bound as a parameter: no
        LOWER(), no LIKE, no normalisation of the input. `workspaces_slug_format`
        already confines every stored slug to lowercase, so a slug differing
        only in case is not a workspace this database can hold. Folding it
        onto a real tenant would be case-insensitive addressing of tenants,
        which is exactly what that constraint exists to prevent; an unknown
        slug must resolve to nothing instead.

        No LIMIT 1: `workspaces_slug_key` makes a second matching row
        impossible, and a limit here would claim doubt about a guarantee the
        schema already gives.
        """
        row = await connection.fetchrow(
            """
            SELECT id
            FROM workspaces
            WHERE slug = $1
            """,
            slug,
        )

        if row is None:
            return None

        # Annotated rather than returned inline: asyncpg ships no types, so
        # `row["id"]` is Any and would silently satisfy any return type.
        workspace_id: UUID = row["id"]

        return workspace_id
