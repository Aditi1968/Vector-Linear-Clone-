from uuid import UUID

from graphql import GraphQLError
from strawberry.types import Info


# The message an unauthenticated caller is given, as a constant because it is
# a contract rather than prose. UNAUTHENTICATED is in
# `app.graphql.schema.PUBLIC_ERROR_CODES`, which means every message raised
# under it reaches clients verbatim.
UNAUTHENTICATED_MESSAGE = "Authentication required"


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
