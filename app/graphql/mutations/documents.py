import strawberry
from strawberry.types import Info

from app.domain.errors import ValidationError
from app.domain.patch import UNSET
from app.graphql.inputs.document import (
    DocumentCommentCreateInput,
    DocumentCommentDeleteInput,
    DocumentCreateInput,
    DocumentDeleteInput,
    DocumentEditInput,
    DocumentRestoreInput,
)
from app.graphql.scope import authorized_scope
from app.graphql.types.document import (
    DocumentCommentDeletePayload,
    DocumentCommentPayload,
    DocumentCommentType,
    DocumentDeletePayload,
    DocumentPayload,
    DocumentType,
)
from app.graphql.types.errors import ValidationErrorType


def _errors(exc: ValidationError) -> list[ValidationErrorType]:
    return [ValidationErrorType.from_domain(issue) for issue in exc.issues]


@strawberry.type
class DocumentMutation:
    """The write half of documents, merged into the root Mutation.

    Every resolver begins by establishing WHO is acting, before it touches a
    service, and that ordering is the point rather than a style: an
    unauthenticated request must perform no protected data lookup at all, so
    the UNAUTHENTICATED error is raised while the request is still just a
    session cookie that named nothing. `authorized_scope` does that first, and
    the acting user is then read off the scope it returns -- `scope.user_id` is
    the membership row the database matched, so authorship and permission
    cannot come from two lookups that disagree.

    No resolver accepts a user id. The viewer is the creator on the way in, the
    editor on every write, and the only permitted deleter of their own
    comments; none of those is something a client may assert about itself. The
    four composite author keys in migrations/023_documents.sql are what make it
    true of the database and not merely of this file.

    Every resolver translates ValidationError into `payload.errors` and nothing
    else. asyncpg failures, bugs and outages propagate through GraphQL's normal
    error mechanism and are masked by app/graphql/schema.py -- including
    `InvalidStoredContentError`, which is a defect about a stored row and must
    not be reported to a client as something it can correct.
    """

    @strawberry.mutation
    async def document_create(
        self, info: Info, input: DocumentCreateInput
    ) -> DocumentPayload:
        """Create a document, attributed to the viewer.

        The content is parsed before anything is stored, so a tree carrying a
        node type, a mark or a link scheme this server does not accept comes
        back as a field error on `content` rather than as a row.
        """
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            entity = await info.context.document_service.create(
                scope=scope,
                title=input.title,
                # strawberry's UNSET is the transport's sentinel; the service
                # takes the domain's. Translated here, at the boundary, exactly
                # as every other input type is -- services must not import
                # strawberry. See app/domain/patch.py.
                content=UNSET if input.content is strawberry.UNSET else input.content,
                project_id=input.project_id,
                initiative_id=input.initiative_id,
                creator_id=scope.user_id,
            )
        except ValidationError as exc:
            return DocumentPayload(document=None, errors=_errors(exc))

        return DocumentPayload(
            document=DocumentType.from_entity(entity, scope),
            errors=[],
        )

    @strawberry.mutation
    async def document_edit(
        self, info: Info, input: DocumentEditInput
    ) -> DocumentPayload:
        """Change a document, taking a version snapshot if this is a boundary.

        `snapshot: true` forces one. Whether one is taken anyway is
        `DocumentService.edit`'s decision and is deliberately not exposed as
        three separate mutations -- an author saving is one act, and which
        version boundary it happens to land on is not something a client should
        have to know in order to save.
        """
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            entity = await info.context.document_service.edit(
                scope=scope,
                document_id=input.id,
                editor_id=scope.user_id,
                title=UNSET if input.title is strawberry.UNSET else input.title,
                content=UNSET if input.content is strawberry.UNSET else input.content,
                snapshot=input.snapshot,
            )
        except ValidationError as exc:
            return DocumentPayload(document=None, errors=_errors(exc))

        return DocumentPayload(
            document=DocumentType.from_entity(entity, scope),
            errors=[],
        )

    @strawberry.mutation
    async def document_restore(
        self, info: Info, input: DocumentRestoreInput
    ) -> DocumentPayload:
        """Put a past version of a document back.

        Non-destructive: the version being replaced is snapshotted first, so a
        restore is itself undoable. A revision belonging to another document or
        another workspace answers exactly as one that does not exist.
        """
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            entity = await info.context.document_service.restore(
                scope=scope,
                document_id=input.document_id,
                revision_id=input.revision_id,
                editor_id=scope.user_id,
            )
        except ValidationError as exc:
            return DocumentPayload(document=None, errors=_errors(exc))

        return DocumentPayload(
            document=DocumentType.from_entity(entity, scope),
            errors=[],
        )

    @strawberry.mutation
    async def document_delete(
        self, info: Info, input: DocumentDeleteInput
    ) -> DocumentDeletePayload:
        """Delete a document, its whole history and its discussion.

        A document in another workspace answers exactly as one that does not
        exist, and neither has anything deleted.
        """
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            await info.context.document_service.delete(
                scope=scope,
                document_id=input.id,
            )
        except ValidationError as exc:
            return DocumentDeletePayload(
                deleted_document_id=None,
                errors=_errors(exc),
            )

        return DocumentDeletePayload(deleted_document_id=input.id, errors=[])

    @strawberry.mutation
    async def document_comment_create(
        self, info: Info, input: DocumentCommentCreateInput
    ) -> DocumentCommentPayload:
        """Write a comment on a document, attributed to the viewer.

        A viewer who is not a member of this request's workspace is refused by
        `document_comments_author_fk` and answered "Document does not exist",
        which is the same answer an unknown document id gets. That equivalence
        is deliberate; see `DocumentService`'s constraint table.
        """
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            entity = await info.context.document_service.create_comment(
                scope=scope,
                document_id=input.document_id,
                author_id=scope.user_id,
                body=input.body,
            )
        except ValidationError as exc:
            return DocumentCommentPayload(comment=None, errors=_errors(exc))

        return DocumentCommentPayload(
            comment=DocumentCommentType.from_entity(entity),
            errors=[],
        )

    @strawberry.mutation
    async def document_comment_delete(
        self, info: Info, input: DocumentCommentDeleteInput
    ) -> DocumentCommentDeletePayload:
        """Withdraw one of the viewer's own comments.

        A comment written by somebody else answers exactly as one that does not
        exist -- see `DocumentService.delete_comment` for why the two must not
        be distinguishable. Workspace admins cannot yet delete other people's
        comments; that rule needs a role on this path, and half of it enforced
        here would read as all of it.
        """
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            deleted_id = await info.context.document_service.delete_comment(
                scope=scope,
                comment_id=input.id,
                author_id=scope.user_id,
            )
        except ValidationError as exc:
            return DocumentCommentDeletePayload(
                deleted_comment_id=None,
                errors=_errors(exc),
            )

        return DocumentCommentDeletePayload(
            deleted_comment_id=deleted_id,
            errors=[],
        )
