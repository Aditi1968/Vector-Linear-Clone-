import strawberry
from strawberry.types import Info

from app.graphql.scope import authorized_scope
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

        The resolver stays thin: it authorizes the slug, asks the service for
        the teams, and maps entities to types. The two calls are separate
        because they answer separate questions -- may this caller be here,
        and what does the workspace contain -- and the second must not be
        reachable without the first.

        This field used to answer an unknown slug with an empty list, because
        membership did not exist yet and an error would have been an oracle
        for guessing which slugs were taken. It exists now, so the answer is
        the one every scoped field gives: a workspace that does not exist and
        one the viewer is not a member of are the same NOT_FOUND error, and an
        unauthenticated caller is refused before either is looked up. The
        disclosure property is unchanged; what changed is who gets rows.
        """
        scope = await authorized_scope(info, workspace_slug)

        workflows = await info.context.team_service.list_workflows(scope)

        return [TeamType.from_domain(workflow) for workflow in workflows]
