from collections import defaultdict
from uuid import UUID

from strawberry.dataloader import DataLoader

from app.domain.issues import IssueEntity
from app.domain.tenancy import WorkspaceScope
from app.services.issues import IssueService


# (workspace_id, issue_id), never a bare id.
#
# The tenant is IN the key rather than captured by the loader, for the reason
# every loader in this package gives: a cache keyed on half of what identifies
# a row is free to answer a key loaded under one workspace from a value
# fetched under another, and the only thing stopping it would be that nobody
# had yet written a request that mixes them. Carrying the workspace makes a
# cross-tenant hit unrepresentable rather than merely unreached.
IssueKey = tuple[UUID, UUID]


def build_issue_loader(
    service: IssueService,
) -> DataLoader[IssueKey, IssueEntity | None]:
    """Batch an issue-per-row field across one request.

    `Notification.issue` is what this exists for. An inbox page names
    twenty-five issues and resolving each on its own is twenty-five round
    trips for a screen that shows one line per row -- and the complexity rule
    cannot price it, because the field declares no page size and is charged as
    a single selection (app/graphql/limits.py). What that rule bounds is the
    shape of the document; what this bounds is the number of statements.

    A missing key resolves to None rather than raising. "This id is not a live
    issue in this workspace" is not an error here: it is the same answer
    another tenant's issue gives, and the same one an archived issue gives, so
    the resolver renders all three as a null field and a client learns nothing
    from the difference.

    Grouped by workspace before dispatch even though a request addresses one
    tenant today. The grouping is what makes the scope handed to the service
    derive from the KEY rather than from anything ambient, so this stays
    correct on the day one document resolves two workspaces.
    """

    async def load(keys: list[IssueKey]) -> list[IssueEntity | None]:
        by_workspace: dict[UUID, list[UUID]] = defaultdict(list)

        for workspace_id, issue_id in keys:
            by_workspace[workspace_id].append(issue_id)

        found: dict[IssueKey, IssueEntity] = {}

        for workspace_id, issue_ids in by_workspace.items():
            entities = await service.get_many_by_ids(
                # Reconstructed rather than passed in: a DataLoader key has to
                # be hashable and a scope is not the key, it is part of it.
                # The workspace in the key came from a resolver that held a
                # real, authorized scope, so this rebuilds what that resolver
                # already had rather than inventing a tenant.
                scope=WorkspaceScope(workspace_id=workspace_id),
                issue_ids=issue_ids,
            )

            for entity in entities:
                found[(workspace_id, entity.id)] = entity

        # One result per key, in the order the keys arrived. DataLoader pairs
        # them positionally, so a filtered or re-ordered list here would hand
        # each caller somebody else's issue.
        return [found.get(key) for key in keys]

    return DataLoader(load_fn=load)
