from uuid import UUID

import asyncpg

from app.domain.tenancy import WorkspaceScope


class TeamRepository:
    """SQL access for `teams`.

    The repository receives a connection from the service layer. It never
    acquires connections, never touches the pool, and never owns a
    transaction. `asyncpg.Record` never escapes this class.
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
