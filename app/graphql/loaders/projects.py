from collections import defaultdict
from uuid import UUID

from strawberry.dataloader import DataLoader

from app.domain.projects import ProjectEntity, ProjectMilestoneEntity
from app.domain.tenancy import WorkspaceScope
from app.services.projects import ProjectService


# The key of every loader here is `(workspace_id, entity_id)`, never a bare id.
#
# A loader keyed on the id alone would be a cache indexed by half of what
# identifies a row. Two requests share no loader today -- these are built per
# request -- but the batch function would still be free to answer a key loaded
# under one workspace from a value fetched under another, and the only thing
# stopping it would be that nobody had yet written a request that mixes them.
# Carrying the tenant in the key means a cross-tenant hit is not a bug waiting
# for the right caller; it is unrepresentable.
ProjectKey = tuple[UUID, UUID]


def build_project_loader(
    service: ProjectService,
) -> DataLoader[ProjectKey, ProjectEntity | None]:
    """Batch `Issue.project` across one request.

    Without this, a page of fifty issues in projects issues fifty SELECTs.
    With it, one -- the loader collects the keys resolved in the same tick and
    hands them over as an array.

    A missing key resolves to None rather than raising. "This id is not a
    project in this workspace" is not an error here: it is the same answer
    another tenant's project gives, and the resolver renders it as a null
    `project` field exactly as it would for an issue in no project at all.

    Grouped by workspace before dispatch even though a request addresses one
    tenant today. That is not defensive coding for its own sake -- the
    grouping is what makes the scope passed to the service derive from the key
    rather than from anything ambient, so this stays correct on the day a
    single request resolves two workspaces.
    """

    async def load(keys: list[ProjectKey]) -> list[ProjectEntity | None]:
        by_workspace: dict[UUID, list[UUID]] = defaultdict(list)

        for workspace_id, project_id in keys:
            by_workspace[workspace_id].append(project_id)

        found: dict[ProjectKey, ProjectEntity] = {}

        for workspace_id, project_ids in by_workspace.items():
            entities = await service.get_many_by_ids(
                scope=WorkspaceScope(workspace_id=workspace_id),
                project_ids=project_ids,
            )

            for entity in entities:
                found[(workspace_id, entity.id)] = entity

        # One result per key, in the order the keys arrived. DataLoader pairs
        # them positionally, so a filtered or re-ordered list here would hand
        # each caller somebody else's project.
        return [found.get(key) for key in keys]

    return DataLoader(load_fn=load)


def build_project_milestones_loader(
    service: ProjectService,
) -> DataLoader[ProjectKey, list[ProjectMilestoneEntity]]:
    """Batch `Project.milestones` across one request.

    A project with no milestones -- and a key naming a project in another
    workspace -- both resolve to an empty list, which is the same answer
    ProjectService.list_milestones gives and for the same reason.
    """

    async def load(keys: list[ProjectKey]) -> list[list[ProjectMilestoneEntity]]:
        by_workspace: dict[UUID, list[UUID]] = defaultdict(list)

        for workspace_id, project_id in keys:
            by_workspace[workspace_id].append(project_id)

        found: dict[ProjectKey, list[ProjectMilestoneEntity]] = defaultdict(list)

        for workspace_id, project_ids in by_workspace.items():
            entities = await service.list_milestones_for_projects(
                scope=WorkspaceScope(workspace_id=workspace_id),
                project_ids=project_ids,
            )

            # The repository returns them ordered by (project_id, position,
            # id), so appending in arrival order preserves each project's
            # display order without a second sort.
            for entity in entities:
                found[(workspace_id, entity.project_id)].append(entity)

        # `found` is a defaultdict, so `.get` rather than `[]`: subscripting it
        # would insert an empty list for every miss and quietly grow a cache
        # of keys nobody asked about again.
        return [found.get(key, []) for key in keys]

    return DataLoader(load_fn=load)
