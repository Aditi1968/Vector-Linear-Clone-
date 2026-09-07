import strawberry
from graphql import GraphQLError
from strawberry.types import Info

from app.domain.errors import (
    GithubRepositoriesInUseError,
    WorkspaceAccessDeniedError,
)
from app.graphql.inputs.github import GithubDisconnectInput

# Imported from the query module rather than copied, because both are the same
# authorization boundary and it may only have one definition. See
# `github_scope` for what the shared refusal is hiding and why.
from app.graphql.queries.github import github_scope, not_found_error
from app.graphql.types.github import GithubIntegrationType


@strawberry.type
class GithubMutation:
    """GitHub integration writes, composed into the root Mutation.

    There is no `githubConnect` here, and its absence is deliberate rather
    than unfinished. Connecting requires a `state` this server minted and a
    browser round trip through GitHub, so it is an HTTP redirect flow and
    lives in app/rest/github.py -- which is exactly the case CLAUDE.md
    reserves REST for. A GraphQL mutation that took an installation id would
    be the same flow with the CSRF protection removed.
    """

    @strawberry.mutation
    async def github_disconnect(
        self,
        info: Info,
        input: GithubDisconnectInput,
    ) -> GithubIntegrationType:
        """Forget this workspace's installation, and report what is left.

        Returns the integration rather than a payload with an `errors` list.
        Every failure this mutation has is a refusal rather than a correctable
        input problem -- there is no field a client could fix to be allowed --
        so an errors list would be a field that is always empty and a shape
        that invited someone to put an authorization failure in it.

        Idempotent: disconnecting a workspace that is not connected succeeds
        and answers DISCONNECTED. The alternative is an error that means "you
        are already in the state you asked for", which no client can act on.

        This removes Vector's record only. The app stays installed on GitHub
        until somebody removes it there; see GithubService.disconnect for why
        this server does not claim otherwise.
        """
        scope = await github_scope(info, input.workspace_slug)

        try:
            integration = await info.context.github_service.disconnect(scope)
        except WorkspaceAccessDeniedError:
            raise not_found_error() from None
        except GithubRepositoriesInUseError as exc:
            # The one failure here that IS correctable, which is why it is
            # published rather than masked: the admin deletes the releases and
            # tries again. BAD_USER_INPUT rather than an errors list, because
            # this mutation deliberately returns the integration itself -- see
            # the docstring above.
            raise GraphQLError(
                str(exc), extensions={"code": "BAD_USER_INPUT"}
            ) from None

        return GithubIntegrationType.from_entity(integration)
