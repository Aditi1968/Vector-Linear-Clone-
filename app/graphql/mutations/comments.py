import strawberry
from strawberry.types import Info

from app.domain.errors import ValidationError
from app.graphql.inputs.comment import CommentCreateInput, CommentDeleteInput
from app.graphql.scope import authorized_scope
from app.graphql.types.comment import (
    CommentCreatePayload,
    CommentDeletePayload,
    CommentType,
)
from app.graphql.types.errors import ValidationErrorType


@strawberry.type
class CommentMutation:
    """The write half of comments, merged into the root Mutation.

    Both resolvers begin by establishing WHO is acting, before they touch a
    service, and that ordering is the point rather than a style: an
    unauthenticated request must perform no protected data lookup at all, so
    the UNAUTHENTICATED error is raised while the request is still just a
    session cookie that named nothing. `authorized_scope` does that first, and
    the author is then read off the scope it returns -- `scope.user_id` is the
    membership row the database matched, so authorship and permission cannot
    come from two lookups that disagree.

    Neither resolver accepts an author id. The viewer is the author on the way
    in and the only permitted deleter on the way out, and neither fact is
    something a client may assert about itself -- see `CommentCreateInput`, and
    `comments_author_fk`, which is what makes it true of the database and not
    merely of this file.
    """

    @strawberry.mutation
    async def comment_create(
        self, info: Info, input: CommentCreateInput
    ) -> CommentCreatePayload:
        """Write a comment, attributed to the viewer.

        A viewer who is not a member of this request's workspace is refused by
        `comments_author_fk` and answered "Issue does not exist", which is the
        same answer an unknown issue id gets. That equivalence is deliberate:
        someone with no access to the workspace must not learn from a
        distinguishable error that the issue they named is real. See
        `CommentService.create`.
        """
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            entity = await info.context.comment_service.create(
                scope=scope,
                issue_id=input.issue_id,
                author_id=scope.user_id,
                body=input.body,
            )
        except ValidationError as exc:
            # Only expected input validation is translated into the payload.
            # Everything else (asyncpg failures, bugs, outages) propagates
            # through GraphQL's normal error mechanism and is masked.
            return CommentCreatePayload(
                comment=None,
                errors=[ValidationErrorType.from_domain(issue) for issue in exc.issues],
            )

        return CommentCreatePayload(
            comment=CommentType.from_entity(entity),
            errors=[],
        )

    @strawberry.mutation
    async def comment_delete(
        self, info: Info, input: CommentDeleteInput
    ) -> CommentDeletePayload:
        """Withdraw one of the viewer's own comments.

        A comment written by somebody else answers exactly as one that does not
        exist -- see `CommentService.delete` for why the two must not be
        distinguishable. Workspace admins cannot yet delete other people's
        comments; that rule needs a role on this path, and half of it enforced
        here would read as all of it.
        """
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            deleted_id = await info.context.comment_service.delete(
                scope=scope,
                comment_id=input.id,
                author_id=scope.user_id,
            )
        except ValidationError as exc:
            return CommentDeletePayload(
                deleted_comment_id=None,
                errors=[ValidationErrorType.from_domain(issue) for issue in exc.issues],
            )

        return CommentDeletePayload(deleted_comment_id=deleted_id, errors=[])
