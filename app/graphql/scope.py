from graphql import GraphQLError
from strawberry.types import Info

from app.domain.errors import WorkspaceAccessDeniedError
from app.domain.tenancy import AuthorizedWorkspaceScope
from app.graphql.viewer import viewer_user_id


# One message for a workspace that does not exist and for one the viewer does
# not belong to. A constant because it is a contract rather than prose:
# NOT_FOUND is in `app.graphql.schema.PUBLIC_ERROR_CODES`, so this string
# reaches clients verbatim.
#
# Phrased as a miss rather than a refusal, because a refusal would confirm the
# workspace is real. The server does not know which case it is in -- see
# WorkspaceAccessDeniedError -- so this is the only answer it could give
# truthfully anyway.
WORKSPACE_NOT_FOUND_MESSAGE = "Workspace not found"


async def authorized_scope(info: Info, slug: str) -> AuthorizedWorkspaceScope:
    """The workspace this operation is for, once the viewer may act in it.

    Every workspace-scoped resolver in the schema begins with this call and
    none of them builds a scope any other way. One module rather than one copy
    per feature, for the reason `app.graphql.viewer` is one module: this is
    the check that decides whether a request may see a tenant's data at all,
    and two copies of it are two things that can drift -- the one that drifts
    being the one nobody re-read.

    The order is the security property. The viewer is resolved FIRST, so a
    request with no session is refused before any workspace lookup happens at
    all: an unauthenticated caller cannot use this to learn whether a slug is
    taken, because the slug is never looked up for them.

    Then membership. `authorized_scope_for_slug` answers a slug no workspace
    holds and a workspace the viewer is not a member of with the same
    WorkspaceAccessDeniedError, from a single statement that never returns two
    answers to collapse -- so a non-member cannot distinguish a workspace that
    does not exist from one they may not see. That equivalence is translated
    here into the NOT_FOUND error `myWorkspace` already uses, byte for byte.

    Only that one expected refusal is translated. An asyncpg failure or a bug
    propagates and is masked by app/graphql/schema.py, rather than being
    reported to the client as a missing workspace; `from None` keeps the
    domain exception out of the response.

    The return type is the evidence. Everything downstream takes a
    `WorkspaceScope`, and an `AuthorizedWorkspaceScope` is one -- but it can
    only be built from a row in `workspace_members`, so "was this checked?"
    is answered by reading the one call that produced it.
    """
    user_id = await viewer_user_id(info)

    try:
        # Annotated rather than returned inline: the context attribute is
        # untyped here, so returning it directly would satisfy any return
        # type -- including a plain WorkspaceScope, which is the one thing
        # this function must never hand back.
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
