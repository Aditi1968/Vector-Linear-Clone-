from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from uuid import UUID

from strawberry.dataloader import DataLoader

from app.domain.documents import DocumentContent
from app.domain.tenancy import WorkspaceScope
from app.services.documents import DocumentService


# The key is `(workspace_id, row_id)`, never a bare id, for the reason
# app/graphql/loaders/projects.py states in full: a loader keyed on the id alone
# is a cache indexed by half of what identifies a row, and the only thing
# stopping a cross-tenant hit would be that nobody had yet written a request
# that mixes two workspaces.
ContentKey = tuple[UUID, UUID]

# What a fetcher looks like: one workspace, many ids, a mapping back. Both
# service methods below already have this shape, which is what lets one builder
# serve both.
_Fetch = Callable[..., Awaitable[dict[UUID, DocumentContent]]]


def _build_content_loader(
    fetch: _Fetch,
) -> DataLoader[ContentKey, DocumentContent | None]:
    """Batch a body-per-row field across one request.

    One builder for documents and revisions because the two are the same
    problem: a body that is large, that most queries do not select, and that
    would otherwise be one query per row on any query that does. A second copy
    of this function differing only in which service method it calls would be
    two things to keep in step for no reader's benefit.

    Per request and never wider, which is what makes a cache keyed by
    (workspace, id) safe to hold at all: a loader living longer than one request
    would go on answering with content that has since been edited, and one
    shared between requests would answer one caller with another's batch.

    A key naming another workspace's row, or one that does not exist, resolves
    to None -- the same answer both cases have to produce, since telling them
    apart is exactly the cross-tenant existence check every read here refuses.
    """

    async def load(keys: list[ContentKey]) -> list[DocumentContent | None]:
        by_workspace: dict[UUID, list[UUID]] = defaultdict(list)

        for workspace_id, row_id in keys:
            by_workspace[workspace_id].append(row_id)

        found: dict[ContentKey, DocumentContent] = {}

        for workspace_id, row_ids in by_workspace.items():
            contents = await fetch(
                scope=WorkspaceScope(workspace_id=workspace_id),
                # The keyword differs between the two services methods, so it
                # is bound by the wrappers below rather than named here.
                ids=row_ids,
            )

            for row_id, content in contents.items():
                found[(workspace_id, row_id)] = content

        return [found.get(key) for key in keys]

    return DataLoader(load_fn=load)


def build_document_content_loader(
    service: DocumentService,
) -> DataLoader[ContentKey, DocumentContent | None]:
    """Batches `Document.content` across whatever page of documents asked."""

    async def fetch(
        *,
        scope: WorkspaceScope,
        ids: Sequence[UUID],
    ) -> dict[UUID, DocumentContent]:
        return await service.contents_for_documents(scope=scope, document_ids=ids)

    return _build_content_loader(fetch)


def build_revision_content_loader(
    service: DocumentService,
) -> DataLoader[ContentKey, DocumentContent | None]:
    """Batches `DocumentRevision.content` across one document's history.

    A separate loader from the one above rather than a shared cache with a
    discriminator in the key: a document id and a revision id are drawn from
    different tables and could in principle collide, and a cache that could
    return a revision for a document key would be a bug with no failing test to
    find it.
    """

    async def fetch(
        *,
        scope: WorkspaceScope,
        ids: Sequence[UUID],
    ) -> dict[UUID, DocumentContent]:
        return await service.contents_for_revisions(scope=scope, revision_ids=ids)

    return _build_content_loader(fetch)
