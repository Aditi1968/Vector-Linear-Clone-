from uuid import UUID

from app.domain.tenancy import WorkspaceScope
from app.services.teams import TeamService
from app.services.workspaces import WorkspaceService


# TEMPORARY, and the only hardcoded tenant in the application.
#
# The GraphQL schema has no way to say which workspace an operation is for:
# `issues`, `issue` and `issueCreate` take no workspace argument, and adding
# one is a separate piece of work. Meanwhile the services below this layer
# now require a scope on every call, and rightly so. Something has to bridge
# the two, and the honest bridge is one named constant in the outermost
# layer rather than a default buried in a service or a repository, where it
# would read as a rule instead of as a gap.
#
# A slug and not a UUID, so this still goes through the same resolution
# every real request will use: an unknown slug fails here exactly as it will
# fail for a client-supplied one, and nothing downstream is handed an id
# that never came from `workspaces`.
#
# What replaces it: `workspaceSlug` as a real argument, resolved per
# request. At that point this constant and `RequestTenant.scope` both go,
# and the resolvers ask WorkspaceService directly with the client's slug.
BOOTSTRAP_WORKSPACE_SLUG = "vector"


class RequestTenant:
    """The tenant a resolver operates in, until clients can name one.

    Deliberately a seam rather than a convenience. Every resolver that needs
    a workspace goes through this one object, so the list of places to
    change when `workspaceSlug` arrives is the list of calls into it -- and
    a resolver that quietly acquired a scope some other way would stand out
    rather than blend in.

    Nothing here is cached across calls. A per-request object could memoise
    the lookup safely, but this one exists to be deleted, and a cache is one
    more thing whose lifetime a reader has to reason about before believing
    that two resolvers in one document cannot see two different tenants.
    """

    def __init__(
        self,
        workspace_service: WorkspaceService,
        team_service: TeamService,
    ):
        self._workspace_service = workspace_service
        self._team_service = team_service

    async def scope(self) -> WorkspaceScope:
        """The workspace every operation in this request is bound to."""
        return await self._workspace_service.scope_for_slug(BOOTSTRAP_WORKSPACE_SLUG)

    async def team_id(self, scope: WorkspaceScope) -> UUID:
        """The team new issues are filed against in that workspace.

        Takes the scope rather than resolving one of its own, so that the
        team and the workspace an operation writes into cannot come from two
        separate lookups that disagree.
        """
        return await self._team_service.default_team_id(scope)
