from uuid import UUID

from strawberry.dataloader import DataLoader

from app.domain.cycles import CycleEntity
from app.domain.tenancy import WorkspaceScope
from app.services.cycles import CycleService


# What a batch is keyed by. The workspace travels IN the key rather than
# being captured when the loader is built: a loader that closed over one
# scope would answer from a cache filled under it, and the first resolver
# that ever runs under a second workspace in one request -- which is what
# `workspaceSlug` as a real argument will allow -- would read the first
# one's rows. Keying by the pair makes that impossible instead of unlikely.
# WorkspaceScope is a frozen slotted dataclass, so it hashes by value.
CycleKey = tuple[WorkspaceScope, UUID]


class CycleLoader:
    """Per-request batching for `Issue.cycle`.

    Without it, a page of fifty issues resolves fifty cycles one statement
    at a time -- the N+1 that a list view produces on its very first render,
    since issues in a cycle are the normal case rather than the exception.
    With it, that page issues one statement per distinct workspace, and none
    at all for issues in no cycle.

    Built per request, alongside the context that holds it, and never shared
    between them. A DataLoader caches every key it has resolved for its own
    lifetime; that is exactly right within one request, where two resolvers
    asking for the same cycle should not ask the server twice, and exactly
    wrong across requests, where the second would be served a cycle as it
    looked before the first client edited it.
    """

    def __init__(self, cycle_service: CycleService):
        self._service = cycle_service
        self._loader: DataLoader[CycleKey, CycleEntity | None] = DataLoader(
            load_fn=self._load_batch,
        )

    async def load(
        self,
        *,
        scope: WorkspaceScope,
        cycle_id: UUID,
    ) -> CycleEntity | None:
        """The cycle, or nothing if this workspace has no such cycle."""
        return await self._loader.load((scope, cycle_id))

    async def _load_batch(self, keys: list[CycleKey]) -> list[CycleEntity | None]:
        """One result per key, in the order asked -- the DataLoader contract.

        Grouped by workspace because a scope is not a filter that can be
        ORed with another: two workspaces' ids have to be two statements,
        each scoped to its own tenant, or the batch would be the one place
        in the application that reads across tenancy.

        A key with no row resolves to None rather than raising. An id that
        names nothing and an id belonging to another tenant reach this the
        same way and leave it the same way, which is what keeps the batched
        path from answering a question the single lookup refuses.
        """
        by_scope: dict[WorkspaceScope, list[UUID]] = {}

        for scope, cycle_id in keys:
            by_scope.setdefault(scope, []).append(cycle_id)

        found: dict[CycleKey, CycleEntity] = {}

        for scope, cycle_ids in by_scope.items():
            for entity in await self._service.get_many(
                scope=scope,
                cycle_ids=cycle_ids,
            ):
                found[(scope, entity.id)] = entity

        return [found.get(key) for key in keys]
