from datetime import datetime
from uuid import UUID

import strawberry
from strawberry.scalars import JSON
from strawberry.types import Info

from app.domain.documents import (
    DocumentCommentEntity,
    DocumentCommentPage,
    DocumentEntity,
    DocumentPage,
    DocumentRevisionEntity,
    DocumentRevisionPage,
)
from app.domain.errors import ValidationError
from app.domain.tenancy import WorkspaceScope
from app.graphql.errors import bad_user_input
from app.graphql.types.errors import ValidationErrorType
from app.graphql.types.pagination import PageInfo


# The page size `Document.comments` and `Document.revisions` declare.
#
# Smaller than the 50 `documents` uses, and the difference is the point: both
# fields are NESTED, so their cost is multiplied by the page of documents above
# them. app/graphql/limits.py charges a list field `page size * inner cost` and
# reads the declared default when a document omits the argument, so this number
# is what an unqualified `comments` costs inside every document on a page.
#
# `DEFAULT_COMMENT_FIRST` in types/comment.py is the same number for the same
# reason, and is deliberately not imported: these are two fields whose page
# sizes happen to agree today, not one decision made in two places.
DEFAULT_NESTED_FIRST = 20


@strawberry.type(name="DocumentComment")
class DocumentCommentType:
    """One comment on a document, as the wire sees it.

    `CommentType` with a different parent, and its two decisions unchanged:
    `authorId` and not `author`, because resolving the author to a `User` means
    publishing `UserType.email` to everyone who can read the document; and
    `editedAt` null for a comment nobody has edited, which is a different claim
    from `updatedAt`.
    """

    id: UUID
    document_id: UUID
    author_id: UUID
    body: str
    edited_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_entity(cls, entity: DocumentCommentEntity) -> "DocumentCommentType":
        return cls(
            id=entity.id,
            document_id=entity.document_id,
            author_id=entity.author_id,
            body=entity.body,
            edited_at=entity.edited_at,
            created_at=entity.created_at,
            updated_at=entity.updated_at,
        )


@strawberry.type
class DocumentCommentConnection:
    nodes: list[DocumentCommentType]
    page_info: PageInfo

    @classmethod
    def from_domain(cls, page: DocumentCommentPage) -> "DocumentCommentConnection":
        return cls(
            nodes=[DocumentCommentType.from_entity(entity) for entity in page.nodes],
            page_info=PageInfo(
                has_next_page=page.has_next_page,
                end_cursor=page.end_cursor,
            ),
        )


@strawberry.type(name="DocumentRevision")
class DocumentRevisionType:
    """One superseded version of a document.

    `createdAt` is when this version STOPPED being current, which is the same
    instant the next one started -- so a history reads as a list of "until
    then" markers rather than of edit times. `authorId` is who wrote this
    version, not who replaced it.
    """

    id: UUID
    document_id: UUID
    title: str
    author_id: UUID
    created_at: datetime

    scope: strawberry.Private[WorkspaceScope]

    @strawberry.field
    async def content(self, info: Info) -> JSON | None:
        """This version's body, or null if it is no longer readable.

        Batched, like `Document.content` and for its reason.

        Null covers two cases a client cannot distinguish and does not need to:
        a revision that is not in this workspace -- which the loader answers as
        a miss, exactly as it answers an id that names nothing -- and a
        revision whose stored tree no longer parses, which
        `DocumentRepository` refuses rather than serving. The second is a
        defect, and it is deliberately NOT hidden by returning an empty
        document: a history entry that renders as blank is worse than one that
        renders as absent, because the first looks like a version somebody
        wrote.
        """
        content = await info.context.revision_content_loader.load(
            (self.scope.workspace_id, self.id)
        )

        return None if content is None else content.root

    @classmethod
    def from_entity(
        cls, entity: DocumentRevisionEntity, scope: WorkspaceScope
    ) -> "DocumentRevisionType":
        return cls(
            scope=scope,
            id=entity.id,
            document_id=entity.document_id,
            title=entity.title,
            author_id=entity.author_id,
            created_at=entity.created_at,
        )


@strawberry.type
class DocumentRevisionConnection:
    nodes: list[DocumentRevisionType]
    page_info: PageInfo

    @classmethod
    def from_domain(
        cls, page: DocumentRevisionPage, scope: WorkspaceScope
    ) -> "DocumentRevisionConnection":
        return cls(
            nodes=[
                DocumentRevisionType.from_entity(entity, scope) for entity in page.nodes
            ],
            page_info=PageInfo(
                has_next_page=page.has_next_page,
                end_cursor=page.end_cursor,
            ),
        )


@strawberry.type(name="Document")
class DocumentType:
    """One document.

    `projectId` and `initiativeId` are never both set -- `documents_one_parent`
    refuses that row -- and both null is a workspace-level document, which is
    an ordinary state rather than an orphan.

    Ids and not `Project` / `Initiative` objects, following the argument
    `Initiative.projectIds` makes: a field named `projectId` says what it is
    instead of pretending a resolver is coming, and when one arrives `project:
    Project` is added beside this and this is deprecated -- a change clients can
    see coming.
    """

    id: UUID
    title: str
    project_id: UUID | None
    initiative_id: UUID | None
    creator_id: UUID
    last_edited_by: UUID
    created_at: datetime
    updated_at: datetime

    # Carried, not exposed: `strawberry.Private` keeps it out of the schema.
    #
    # The scope the root field AUTHORIZED, handed down so that a nested
    # resolver runs in the same workspace its parent was read from. It is not
    # taken from the row -- an entity that remembered its own tenant would let
    # this resolver scope a query with a value that arrived from an earlier
    # query's RESULT, which is how a compromised row becomes a key to another
    # workspace.
    scope: strawberry.Private[WorkspaceScope]

    @strawberry.field
    async def content(self, info: Info) -> JSON | None:
        """The document body, as a ProseMirror/TipTap tree.

        A resolver and not a plain field, which is the one structural decision
        in this type. A body may be 200,000 characters, so a page of fifty
        documents that carried them inline would be a ten-megabyte response for
        a screen that renders titles. As a resolver it is fetched only when
        selected, batched across the page by
        app/graphql/loaders/documents.py, and priced by app/graphql/limits.py
        as one field rather than as a hidden fan-out.

        The tree that comes back has been through
        `app.domain.documents.parse_content` on the way out of the repository,
        so a client is never handed a node type, a mark or a link scheme this
        server does not accept -- whatever is actually in the column. That is
        the whole reason this feature stores JSON rather than HTML; see the top
        of migrations/023_documents.sql.

        Null for a document that is not in this workspace and for one whose
        stored tree no longer parses. Both are absences a client renders the
        same way, and the second is a defect that has already been raised
        inside the server rather than something to paper over with an empty
        document.
        """
        content = await info.context.document_content_loader.load(
            (self.scope.workspace_id, self.id)
        )

        return None if content is None else content.root

    @strawberry.field
    async def revisions(
        self,
        info: Info,
        first: int = DEFAULT_NESTED_FIRST,
        after: str | None = None,
    ) -> DocumentRevisionConnection:
        """This document's version history, newest first.

        Paginated rather than capped at a constant, unlike
        `Initiative.updates`: a history grows without bound by design -- that is
        what it is for -- so a limit would be a number at which the oldest
        versions silently stop being reachable.
        """
        try:
            page = await info.context.document_service.list_revisions(
                scope=self.scope,
                document_id=self.id,
                first=first,
                after=after,
            )
        except ValidationError as exc:
            raise bad_user_input("Invalid pagination arguments", exc) from None

        return DocumentRevisionConnection.from_domain(page, self.scope)

    @strawberry.field
    async def comments(
        self,
        info: Info,
        first: int = DEFAULT_NESTED_FIRST,
        after: str | None = None,
    ) -> DocumentCommentConnection:
        """This document's discussion, oldest first."""
        try:
            page = await info.context.document_service.list_comments(
                scope=self.scope,
                document_id=self.id,
                first=first,
                after=after,
            )
        except ValidationError as exc:
            raise bad_user_input("Invalid pagination arguments", exc) from None

        return DocumentCommentConnection.from_domain(page)

    @classmethod
    def from_entity(
        cls, entity: DocumentEntity, scope: WorkspaceScope
    ) -> "DocumentType":
        return cls(
            scope=scope,
            id=entity.id,
            title=entity.title,
            project_id=entity.project_id,
            initiative_id=entity.initiative_id,
            creator_id=entity.creator_id,
            last_edited_by=entity.last_edited_by,
            created_at=entity.created_at,
            updated_at=entity.updated_at,
        )


@strawberry.type
class DocumentConnection:
    nodes: list[DocumentType]
    page_info: PageInfo

    @classmethod
    def from_domain(
        cls, page: DocumentPage, scope: WorkspaceScope
    ) -> "DocumentConnection":
        return cls(
            nodes=[DocumentType.from_entity(entity, scope) for entity in page.nodes],
            page_info=PageInfo(
                has_next_page=page.has_next_page,
                end_cursor=page.end_cursor,
            ),
        )


# One payload type per SHAPE, reused across the mutations that share it, rather
# than one per mutation -- the convention app/graphql/types/project.py argues
# for at length. The first time one of these needs a field the others do not,
# it gets its own type.
@strawberry.type
class DocumentPayload:
    document: DocumentType | None
    errors: list[ValidationErrorType]


@strawberry.type
class DocumentDeletePayload:
    # The id rather than the document. Returning the deleted row would invite a
    # client to render something that no longer exists; the id is what a cache
    # needs in order to evict it.
    deleted_document_id: UUID | None
    errors: list[ValidationErrorType]


@strawberry.type
class DocumentCommentPayload:
    comment: DocumentCommentType | None
    errors: list[ValidationErrorType]


@strawberry.type
class DocumentCommentDeletePayload:
    """What was removed, so a client can evict it without guessing.

    Null when nothing was deleted -- which is also the answer for a comment
    that belongs to another workspace or to another author, because those must
    not be distinguishable from one that never existed.
    """

    deleted_comment_id: UUID | None
    errors: list[ValidationErrorType]
