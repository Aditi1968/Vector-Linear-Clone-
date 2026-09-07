from uuid import UUID

import strawberry
from strawberry.types import Info

from app.domain.documents import DocumentFilter
from app.domain.errors import ValidationError
from app.graphql.errors import bad_user_input
from app.graphql.scope import authorized_scope
from app.graphql.types.document import DocumentConnection, DocumentType


DEFAULT_FIRST = 50


@strawberry.type
class DocumentQuery:
    """The read half of the documents API.

    Merged into the schema's single `Query` by app/graphql/schema.py. Kept as
    its own class so that the fields of a feature live with that feature and
    two people adding queries at once are not editing the same class.
    """

    @strawberry.field
    async def document(
        self,
        info: Info,
        workspace_slug: str,
        id: UUID,
    ) -> DocumentType | None:
        """One document, or null.

        The slug is authorized before the id selects anything. A document in
        another workspace resolves to null, the same answer as an id that
        exists nowhere, so this cannot be used to ask whether someone else's
        document exists.

        The body is not fetched here. `Document.content` is a resolver over a
        batching loader, so a client that only wants the title pays for a title.
        """
        scope = await authorized_scope(info, workspace_slug)

        entity = await info.context.document_service.get_by_id(
            scope=scope,
            document_id=id,
        )

        if entity is None:
            return None

        return DocumentType.from_entity(entity, scope)

    @strawberry.field
    async def documents(
        self,
        info: Info,
        workspace_slug: str,
        project_id: UUID | None = strawberry.UNSET,
        initiative_id: UUID | None = strawberry.UNSET,
        first: int = DEFAULT_FIRST,
        after: str | None = None,
    ) -> DocumentConnection:
        """A keyset page of the workspace's documents, newest first.

        The two filters are tri-state, and the third state is the reason they
        are `UNSET`-defaulted rather than plain nullable arguments:

          * omitted     -- every document in the workspace;
          * an id       -- the documents on that project or initiative;
          * explicit null -- the documents on NO project (or no initiative),
            which is how "the workspace's own documents" is asked for.

        A plain `projectId: ID` could not express the third at all, and would
        silently answer it as the first.

        An id from another workspace narrows to nothing and returns an empty
        page rather than an error, because an error would tell a caller holding
        a guessed id that the project is real.
        """
        scope = await authorized_scope(info, workspace_slug)

        # `strawberry.UNSET` is falsy and is not None, so the two tests below
        # are genuinely different questions: "did the client mention this
        # field" and "did the client send null".
        document_filter = DocumentFilter(
            project_id=None if project_id is strawberry.UNSET else project_id,
            initiative_id=None if initiative_id is strawberry.UNSET else initiative_id,
            by_project=project_id is not strawberry.UNSET,
            by_initiative=initiative_id is not strawberry.UNSET,
        )

        try:
            page = await info.context.document_service.list(
                scope=scope,
                document_filter=document_filter,
                first=first,
                after=after,
            )
        except ValidationError as exc:
            # Only expected pagination input errors are translated. Anything
            # else (asyncpg failures, bugs) propagates as a real execution
            # error. `from None` keeps parser detail out of the response.
            raise bad_user_input("Invalid pagination arguments", exc) from None

        return DocumentConnection.from_domain(page, scope)
