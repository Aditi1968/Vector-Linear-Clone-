import strawberry
from graphql import GraphQLError
from strawberry.types import Info

from app.domain.errors import (
    GithubRepositoriesInUseError,
    ValidationError,
    ValidationIssue,
    WorkspaceAccessDeniedError,
)
from app.graphql.inputs.github import (
    GithubAutomationSetInput,
    GithubDisconnectInput,
    GithubRepositoriesSetInput,
)

# Imported from the query module rather than copied, because both are the same
# authorization boundary and it may only have one definition. See
# `github_scope` for what the shared refusal is hiding and why.
from app.graphql.queries.github import github_scope, not_found_error
from app.graphql.types.errors import ValidationErrorType
from app.graphql.types.github import GithubIntegrationPayload, GithubIntegrationType


# The one repository id shape a client can get wrong, and the field to blame.
#
# `GithubRepository.repositoryId` is a GraphQL ID, which serialises as a string
# -- deliberately, because GitHub's ids outgrow a signed 32-bit Int -- so the
# transport is where they turn back into integers and the transport is where a
# non-numeric one has to be refused. Left as a 500 it would be a ValueError on
# a value a client typed.
_BAD_REPOSITORY_ID = ValidationIssue(
    field="repositoryIds",
    code="INVALID",
    message="Repository ids must be GitHub's own numeric ids.",
)


def _errors(exc: ValidationError) -> list[ValidationErrorType]:
    return [ValidationErrorType.from_domain(issue) for issue in exc.issues]


def _repository_ids(values: list[strawberry.ID]) -> list[int]:
    """GitHub's numeric ids, or a field error naming the list they came in.

    Not `int(value)` inline at the call site: every id has to parse before any
    of them is used, so that a list with one bad entry is refused whole rather
    than applied up to the mistake.
    """
    try:
        return [int(value) for value in values]
    except ValueError:
        raise ValidationError([_BAD_REPOSITORY_ID]) from None


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

    @strawberry.mutation
    async def github_repositories_set(
        self,
        info: Info,
        input: GithubRepositoriesSetInput,
    ) -> GithubIntegrationPayload:
        """Choose which repositories this workspace tracks.

        A narrowing of what GitHub already granted. Deliveries about a
        repository that is not in this set are dropped in silence -- exactly as
        deliveries about a repository the installation never covered are -- so
        a workspace with two hundred repositories can take development activity
        from the six it cares about.

        Untracking a repository a release names is refused and answered as a
        field error on `repositoryIds`, for the reason `githubDisconnect` gets
        the same refusal: migration 024 made releases RESTRICT against
        `github_repositories` so that shipping history is not silently detached
        by a toggle elsewhere, and untracking is that toggle. It is a field
        error rather than a top-level one because this payload HAS a field to
        blame, which is the difference between highlighting the checkbox and a
        toast saying something failed.

        History already collected is kept. Untracking stops new deliveries; it
        does not rewrite what an issue's Development section has been showing.
        """
        scope = await github_scope(info, input.workspace_slug)

        try:
            integration = await info.context.github_service.set_tracked_repositories(
                scope,
                repository_ids=_repository_ids(input.repository_ids),
            )
        except WorkspaceAccessDeniedError:
            raise not_found_error() from None
        except ValidationError as exc:
            return GithubIntegrationPayload(integration=None, errors=_errors(exc))
        except GithubRepositoriesInUseError as exc:
            return GithubIntegrationPayload(
                integration=None,
                errors=[
                    ValidationErrorType(
                        field="repositoryIds",
                        code="IN_USE",
                        message=str(exc),
                    )
                ],
            )

        return GithubIntegrationPayload(
            integration=GithubIntegrationType.from_entity(integration),
            errors=[],
        )

    @strawberry.mutation
    async def github_automation_set(
        self,
        info: Info,
        input: GithubAutomationSetInput,
    ) -> GithubIntegrationPayload:
        """Say what a pull request does to one team's issues.

        Opt-in, per team, and off until an admin turns it on -- an issue that
        changed status because somebody opened a pull request, in a workspace
        that never asked for that, is a bug report rather than a feature.

        The states cannot be hardcoded and are not guessed. Migration 005 makes
        workflow states team-scoped and user-named, so there is no global "In
        Progress" and a team may have three states of category `started`; the
        category is the only portable concept and it does not narrow to one
        row. Turning the automation on without naming states stores the team's
        first started and first completed state EXPLICITLY, so what will happen
        is visible on this screen rather than decided later by a rule.

        `enabled: false` removes the configuration. Idempotent.
        """
        scope = await github_scope(info, input.workspace_slug)

        try:
            integration = await info.context.github_service.set_issue_automation(
                scope,
                team_id=input.team_id,
                enabled=input.enabled,
                started_state_id=input.started_state_id,
                completed_state_id=input.completed_state_id,
            )
        except WorkspaceAccessDeniedError:
            raise not_found_error() from None
        except ValidationError as exc:
            # Only expected input validation reaches the payload. A team id
            # naming another tenant's team violates
            # `github_issue_automations_team_fk`, which is not client input in
            # any sense this layer can correct -- the scope was already
            # authorized -- and propagates to be masked.
            return GithubIntegrationPayload(integration=None, errors=_errors(exc))

        return GithubIntegrationPayload(
            integration=GithubIntegrationType.from_entity(integration),
            errors=[],
        )
