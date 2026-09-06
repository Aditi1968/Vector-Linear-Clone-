import strawberry
from strawberry.types import Info

from app.domain.errors import WorkspaceNotFoundError
from app.graphql.types.team import TeamType


@strawberry.type
class TeamQuery:
    @strawberry.field(
        description=(
            "The teams in a workspace, each with the workflow states its "
            "issues can occupy."
        )
    )
    async def teams(self, info: Info, workspace_slug: str) -> list[TeamType]:
        """Teams in a workspace, addressed by slug.

        The resolver stays thin: it resolves the slug, asks the service for
        the teams, and maps entities to types. The two service calls are
        separate because they answer separate questions -- which workspace
        is this, and what does it contain -- and the second must not be
        reachable without the first.

        An unknown slug returns an empty list rather than an error, and the
        choice is about what an error would tell the caller. Authentication
        and membership are not implemented yet; when they are, a caller who
        is not a member of `workspace_slug` must not be able to distinguish
        "that workspace does not exist" from "you cannot see it", or the
        query becomes an oracle for guessing which workspace slugs are
        taken. Answering both with the same empty list today is what makes
        adding the membership check later a change to who gets rows, rather
        than a change to what the API discloses.

        The cost is that a genuine typo is silent. That is the intended
        trade: a slug the caller controls is cheap for them to re-check, and
        the alternative discloses the existence of every tenant.
        """
        try:
            scope = await info.context.workspace_service.scope_for_slug(workspace_slug)
        except WorkspaceNotFoundError:
            return []

        workflows = await info.context.team_service.list_workflows(scope)

        return [TeamType.from_domain(workflow) for workflow in workflows]
