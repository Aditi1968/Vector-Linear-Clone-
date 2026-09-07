from collections import defaultdict
from uuid import UUID

from strawberry.dataloader import DataLoader

from app.domain.initiatives import InitiativeUpdateEntity
from app.domain.tenancy import WorkspaceScope
from app.services.initiatives import InitiativeService


# The key is `(workspace_id, initiative_id)`, never a bare id, for the reason
# app/graphql/loaders/projects.py states in full: a loader keyed on the id
# alone is a cache indexed by half of what identifies a row, and the only thing
# stopping a cross-tenant hit would be that nobody had yet written a request
# that mixes two workspaces.
InitiativeKey = tuple[UUID, UUID]


def build_initiative_updates_loader(
    service: InitiativeService,
) -> DataLoader[InitiativeKey, list[InitiativeUpdateEntity]]:
    """Batch `Initiative.updates` across one request.

    An initiative with no updates -- and a key naming an initiative in another
    workspace -- both resolve to an empty list, which is the same answer
    InitiativeService.list_updates gives and for the same reason.
    """

    async def load(
        keys: list[InitiativeKey],
    ) -> list[list[InitiativeUpdateEntity]]:
        by_workspace: dict[UUID, list[UUID]] = defaultdict(list)

        for workspace_id, initiative_id in keys:
            by_workspace[workspace_id].append(initiative_id)

        found: dict[InitiativeKey, list[InitiativeUpdateEntity]] = defaultdict(list)

        for workspace_id, initiative_ids in by_workspace.items():
            entities = await service.list_updates_for_initiatives(
                scope=WorkspaceScope(workspace_id=workspace_id),
                initiative_ids=initiative_ids,
            )

            # The repository returns them ordered by (initiative_id,
            # created_at DESC, id DESC), so appending in arrival order
            # preserves each initiative's newest-first order without a second
            # sort.
            for entity in entities:
                found[(workspace_id, entity.initiative_id)].append(entity)

        # `found` is a defaultdict, so `.get` rather than `[]`: subscripting it
        # would insert an empty list for every miss and quietly grow a cache of
        # keys nobody asked about again.
        return [found.get(key, []) for key in keys]

    return DataLoader(load_fn=load)
