from uuid import UUID

from strawberry.dataloader import DataLoader

from app.domain.labels import LabelEntity
from app.domain.tenancy import WorkspaceScope
from app.services.labels import LabelService


# (workspace_id, issue_id). The tenant is IN the key rather than captured by
# the loader, and that is the whole reason this is safe to share for the
# length of a request. A loader keyed by issue id alone would cache the labels
# of issue X under a key that says nothing about whose issue X is, so the
# first workspace to ask would answer for every later one -- which is a
# cross-tenant read produced by a cache rather than by a query.
IssueLabelKey = tuple[UUID, UUID]


def issue_label_loader(
    service: LabelService,
) -> DataLoader[IssueLabelKey, list[LabelEntity]]:
    """Batch `Issue.labels` across one page of issues.

    Without this, a page of 50 issues selecting `labels` issues 50 queries --
    the classic N+1, and one the operation-complexity rule does not price,
    because `labels` declares no page-size argument and is charged as a single
    field (app/graphql/limits.py). The list is bounded per issue by
    `app.services.labels.LABELS_PER_ISSUE_MAX`; the number of QUERIES is what this
    bounds.

    One statement per distinct workspace in the batch. In practice that is
    always one, because `RequestTenant` resolves a single workspace per
    request -- but the loader does not assume it, because the day a document
    can name two workspaces, an assumption made here would show up as one
    tenant's issues being looked up under another's key.

    A key the batch found nothing for maps to an empty list. That covers both
    an issue with no labels and an issue that does not exist in this
    workspace, and the two must stay indistinguishable: an id that answered
    differently because it belonged to someone else would report that it is
    real.
    """

    async def load(keys: list[IssueLabelKey]) -> list[list[LabelEntity]]:
        by_workspace: dict[UUID, list[UUID]] = {}

        for workspace_id, issue_id in keys:
            by_workspace.setdefault(workspace_id, []).append(issue_id)

        found: dict[IssueLabelKey, list[LabelEntity]] = {}

        for workspace_id, issue_ids in by_workspace.items():
            grouped = await service.labels_for_issues(
                # Reconstructed rather than passed in, because a DataLoader
                # key has to be hashable and a scope is not the key -- it is
                # part of it. The workspace in the key came from a resolver
                # that held a real scope, so this rebuilds what that resolver
                # already had rather than inventing a tenant.
                scope=WorkspaceScope(workspace_id=workspace_id),
                issue_ids=issue_ids,
            )

            for issue_id, labels in grouped.items():
                found[(workspace_id, issue_id)] = labels

        # Positional: a DataLoader contract is one result per key, in the
        # order the keys arrived. Returning the dict, or a shorter list, would
        # hand each caller somebody else's answer.
        return [found.get(key, []) for key in keys]

    return DataLoader(load_fn=load)
