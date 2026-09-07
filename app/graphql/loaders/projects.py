from collections import defaultdict
from uuid import UUID

from strawberry.dataloader import DataLoader

from app.domain.projects import (
    ProjectDependencies,
    ProjectEntity,
    ProjectMilestoneEntity,
    ProjectUpdateEntity,
)
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


def build_project_updates_loader(
    service: ProjectService,
) -> DataLoader[ProjectKey, list[ProjectUpdateEntity]]:
    """Batch `Project.updates` across one request.

    A project with no updates -- and a key naming a project in another
    workspace -- both resolve to an empty list, which is the same answer
    ProjectService.list_updates gives and for the same reason.
    """

    async def load(keys: list[ProjectKey]) -> list[list[ProjectUpdateEntity]]:
        by_workspace: dict[UUID, list[UUID]] = defaultdict(list)

        for workspace_id, project_id in keys:
            by_workspace[workspace_id].append(project_id)

        found: dict[ProjectKey, list[ProjectUpdateEntity]] = defaultdict(list)

        for workspace_id, project_ids in by_workspace.items():
            entities = await service.list_updates_for_projects(
                scope=WorkspaceScope(workspace_id=workspace_id),
                project_ids=project_ids,
            )

            # The repository returns them ordered by (project_id,
            # created_at DESC, id DESC), so appending in arrival order
            # preserves each project's newest-first order without a second
            # sort.
            for entity in entities:
                found[(workspace_id, entity.project_id)].append(entity)

        return [found.get(key, []) for key in keys]

    return DataLoader(load_fn=load)


# What a project with no dependencies at either end resolves to.
#
# A module-level constant rather than a fresh pair of empty tuples per miss:
# `ProjectDependencies` is frozen and carries tuples, so one instance is safe
# to share and there is nothing a caller could mutate through it. It is also
# what makes the field total -- a key naming another workspace's project gets
# this, the same answer a real project with no dependencies gets.
_NO_DEPENDENCIES = ProjectDependencies(blocks=(), blocked_by=())


def build_project_dependencies_loader(
    service: ProjectService,
) -> DataLoader[ProjectKey, ProjectDependencies]:
    """Batch `Project.dependencies` across one request.

    Both directions come back together, because the repository reads them in
    one statement: two loaders would issue it twice for a field a client almost
    always selects whole.
    """

    async def load(keys: list[ProjectKey]) -> list[ProjectDependencies]:
        by_workspace: dict[UUID, list[UUID]] = defaultdict(list)

        for workspace_id, project_id in keys:
            by_workspace[workspace_id].append(project_id)

        found: dict[ProjectKey, ProjectDependencies] = {}

        for workspace_id, project_ids in by_workspace.items():
            dependencies = await service.list_dependencies_for_projects(
                scope=WorkspaceScope(workspace_id=workspace_id),
                project_ids=project_ids,
            )

            for project_id, entry in dependencies.items():
                found[(workspace_id, project_id)] = entry

        # One result per key, in the order the keys arrived. DataLoader pairs
        # them positionally, so a filtered or re-ordered list here would hand
        # each caller somebody else's dependencies.
        return [found.get(key, _NO_DEPENDENCIES) for key in keys]

    return DataLoader(load_fn=load)
