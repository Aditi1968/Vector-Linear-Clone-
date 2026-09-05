import asyncpg

from app.domain.errors import WorkspaceNotFoundError
from app.domain.tenancy import WorkspaceScope
from app.repositories.workspaces import WorkspaceRepository


class WorkspaceService:
    """Business rules for workspaces.

    Turning a client-supplied slug into a workspace identity happens here and
    nowhere else, so that HTTP, GraphQL and any future worker all resolve a
    tenant the same way. The service also owns connection acquisition and
    transaction boundaries.

    Resolution answers existence and stops there. Whether the caller may act
    in the workspace it resolved is a separate question, asked elsewhere;
    see WorkspaceScope on why holding one grants nothing.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        repository: WorkspaceRepository,
    ):
        self._pool = pool
        self._repository = repository

    async def scope_for_slug(self, slug: str) -> WorkspaceScope:
        """Resolve a slug to the scope later operations are bound to.

        A single SELECT needs no explicit write transaction, so this acquires
        a connection without opening one, and releases it before deciding
        what the lookup means.

        The scope is returned rather than recorded anywhere. One process
        serves many workspaces concurrently, so a module-level "current
        workspace" would be a value the next request overwrites -- and the
        request it overwrote would keep running, now reading another tenant's
        data. Tenant identity is only safe as an explicit argument.

        The raw slug reaches exactly two frames: this method and the lookup
        it delegates to. Past this point tenant-owned work is handed a
        WorkspaceScope and never the string it came from.
        """
        async with self._pool.acquire() as connection:
            workspace_id = await self._repository.find_id_by_slug(connection, slug)

        if workspace_id is None:
            raise WorkspaceNotFoundError()

        return WorkspaceScope(workspace_id=workspace_id)
