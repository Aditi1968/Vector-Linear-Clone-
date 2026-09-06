import strawberry
from graphql import GraphQLError
from strawberry.types import Info

from app.domain.errors import WorkspaceAccessDeniedError
from app.domain.tenancy import AuthorizedWorkspaceScope
from app.graphql.queries.memberships import WORKSPACE_NOT_FOUND_MESSAGE
from app.graphql.types.github import GithubIntegrationType
from app.graphql.viewer import viewer_user_id


async def github_scope(info: Info, workspace_slug: str) -> AuthorizedWorkspaceScope:
    """Resolve (slug, viewer) to a scope, or refuse with NOT_FOUND.

    Shared by the query and the mutation, and it is one function rather than
    two copies because it is the whole authorization boundary of this feature:
    a second copy is a second thing that can drift, and the one that drifts is
    the one nobody re-read.

    Three refusals arrive here as one answer, and they must stay one. A slug
    that names no workspace, a workspace the viewer does not belong to, and a
    workspace where the viewer is an ordinary member all raise
    WorkspaceAccessDeniedError -- the first two from the membership lookup
    (which never computes the difference; see that error) and the third from
    app.services.github.require_workspace_admin. Reporting the third
    distinctly would tell
    every member of a workspace whether their organisation has connected a
    GitHub account, which is the admins' business.

    Not scoped through `info.context.tenant`. That seam resolves whichever
    workspace the request is nominally in and answers a WorkspaceScope, which
    is an identity and not a permission; this needs the authorized kind, which
    only a row in `workspace_members` can produce.
    """
    user_id = await viewer_user_id(info)

    try:
        # Annotated rather than returned inline: the context attribute is
        # untyped here, so returning it directly would satisfy any return type
        # -- including the unauthorized WorkspaceScope this must never be.
        scope: AuthorizedWorkspaceScope = (
            await info.context.membership_service.authorized_scope_for_slug(
                slug=workspace_slug,
                user_id=user_id,
            )
        )
    except WorkspaceAccessDeniedError:
        raise not_found_error() from None

    return scope


def not_found_error() -> GraphQLError:
    """The one answer every refusal in this feature gets.

    The same message `myWorkspace` uses, from the same constant, because two
    fields that answer differently about the same workspace are two halves of
    a probe.
    """
    return GraphQLError(
        WORKSPACE_NOT_FOUND_MESSAGE,
        extensions={"code": "NOT_FOUND"},
    )


@strawberry.type
class GithubQuery:
    """Root query fields for the GitHub integration."""

    @strawberry.field
    async def github_integration(
        self,
        info: Info,
        workspace_slug: str,
    ) -> GithubIntegrationType:
        """One workspace's GitHub integration, for an admin or owner of it.

        Non-null, and it never returns null for a workspace that has not
        connected anything: "not connected" is a status, not an absent field.
        A nullable field would make a workspace with no integration
        indistinguishable from one the caller may not see, and the client
        needs to render those two very differently.

        Nothing this field can return is a secret. The status is derived from
        whether the deployment holds credentials, never from the credentials
        themselves -- see GithubService._view -- so a client learns that a
        GitHub App is configured and never one byte of it.
        """
        scope = await github_scope(info, workspace_slug)

        try:
            integration = await info.context.github_service.integration_for(scope)
        except WorkspaceAccessDeniedError:
            # The role refusal, raised by the service rather than the
            # membership lookup. Same error, same message, deliberately.
            raise not_found_error() from None

        return GithubIntegrationType.from_entity(integration)
