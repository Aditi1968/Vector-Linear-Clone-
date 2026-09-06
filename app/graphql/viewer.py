from uuid import UUID

from graphql import GraphQLError
from strawberry.types import Info

from app.domain.errors import WorkspaceAccessDeniedError
from app.domain.tenancy import AuthorizedWorkspaceScope


# The message an unauthenticated caller is given, as a constant because it is
# a contract rather than prose. UNAUTHENTICATED is in
# `app.graphql.schema.PUBLIC_ERROR_CODES`, which means every message raised
# under it reaches clients verbatim.
UNAUTHENTICATED_MESSAGE = "Authentication required"

# One message for a workspace that does not exist and for one the viewer does
# not belong to, and the same string
# `app.graphql.queries.memberships.WORKSPACE_NOT_FOUND_MESSAGE` publishes --
# two spellings of one refusal would be a way to tell the two cases apart.
# Phrased as a miss rather than a refusal, because a refusal confirms the
# workspace is real.
WORKSPACE_NOT_FOUND_MESSAGE = "Workspace not found"


async def viewer_user_id(info: Info) -> UUID:
    """The authenticated viewer, or an UNAUTHENTICATED error.

    Fails closed and fails first. The viewer is resolved from the session
    cookie the request actually presented, and is None when it presented none,
    presented an expired one, or presented one that names no session. None
    means UNAUTHENTICATED in all three cases: the resolver is not told which,
    and must not be, because a client that could tell an expired session from a
    forged one can probe for live sessions.

    The alternative shapes are both accidents waiting. Reading a user id out of
    a field argument would let any caller name any user -- which for a comment
    is an impersonation API, and is why `CommentCreateInput` has no `authorId`.
    Treating an absent viewer as an empty result would answer an
    unauthenticated stranger the way a successfully authenticated account that
    owns nothing is answered.

    Raised before any service is touched, so an unauthenticated request
    performs no protected data lookup at all. `context.viewer()` is memoised
    per request, so several protected fields in one document share a single
    session lookup rather than each making their own.

    One module rather than one copy per feature, deliberately. This is the
    check that decides whether a request has an identity; two copies of it are
    two things that can drift, and the one that drifts is the one nobody
    re-read.
    """
    viewer = await info.context.viewer()

    if viewer is None:
        raise GraphQLError(
            UNAUTHENTICATED_MESSAGE,
            extensions={"code": "UNAUTHENTICATED"},
        )

    # Annotated rather than returned inline: the context attribute is untyped
    # here, so returning it directly would satisfy any return type.
    identified: UUID = viewer.id

    return identified


async def actor_user_id(info: Info) -> UUID | None:
    """Who to record as having done this, or nobody.

    The permissive half of the pair above, for the mutations that do not yet
    require an identity -- labels, relations, issue edits. They record WHO
    acted in the history and are answerable without knowing, so None here is
    "a change with no actor" rather than a refusal, which is the same state
    `issues.creator_id` has allowed since 006 and the same one a system action
    is in.

    It is deliberately NOT a fallback for `viewer_user_id`. Anything that
    needs an identity to be correct -- writing a comment, reading an inbox --
    calls that one and fails closed. This exists so that a mutation which is
    open today records an honest actor when a session happens to be present,
    instead of every history row claiming nobody did anything.

    Shares the memoised `context.viewer()`, so a document mixing both pays for
    one session lookup.
    """
    viewer = await info.context.viewer()

    if viewer is None:
        return None

    identified: UUID = viewer.id

    return identified


async def authorized_scope(info: Info, slug: str) -> AuthorizedWorkspaceScope:
    """The workspace this request may act in, or a refusal, in that order.

    Two steps, and the order is the security property rather than a style.
    `viewer_user_id` runs first and raises UNAUTHENTICATED before any lookup,
    so an anonymous request performs no protected data access at all; only
    then is the slug resolved, and it is resolved AGAINST the caller -- a
    membership row, not a workspace row. A slug naming a real workspace the
    caller does not belong to therefore reaches nothing.

    The scope it returns carries `user_id` and `role` from the row the
    database matched, which is what makes it an `AuthorizedWorkspaceScope`
    and what everything downstream annotates when it must not be reachable by
    a non-member. See `MembershipService.authorized_scope_for_slug`.

    Only `WorkspaceAccessDeniedError` is translated. An asyncpg failure or a
    bug is not a missing workspace and propagates to be masked; `from None`
    keeps the domain exception out of the response.

    Interim, and knowingly so: app/graphql/scope.py is the intended home for
    this helper, and this lives in the module that already owns "who is
    asking" until that lands.
    """
    user_id = await viewer_user_id(info)

    try:
        # Annotated rather than returned inline, as `viewer_user_id` is: the
        # context attribute is untyped here, so returning it directly would
        # satisfy any return type.
        scope: AuthorizedWorkspaceScope = (
            await info.context.membership_service.authorized_scope_for_slug(
                slug=slug,
                user_id=user_id,
            )
        )
    except WorkspaceAccessDeniedError:
        raise GraphQLError(
            WORKSPACE_NOT_FOUND_MESSAGE,
            extensions={"code": "NOT_FOUND"},
        ) from None

    return scope
