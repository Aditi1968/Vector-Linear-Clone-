from uuid import UUID

import strawberry
from strawberry.types import Info

from app.graphql.scope import authorized_scope
from app.graphql.types.template import IssueTemplateType


@strawberry.type
class TemplateQuery:
    """The read half of templates, merged into the root Query.

    Both fields take a `workspaceSlug` and resolve it against the caller's
    memberships before touching data, so an unauthenticated request is refused
    while it is still just a cookie that named nothing.
    """

    @strawberry.field
    async def issue_templates(
        self,
        info: Info,
        workspace_slug: str,
        team_id: UUID | None = None,
    ) -> list[IssueTemplateType]:
        """The templates one team may file from, in menu order.

        The workspace's shared templates PLUS that team's own, which is why
        this is one field rather than two. Omitting `teamId` asks for the
        shared ones alone -- the "no team chosen yet" state of a create form.

        No connection and no cursor: a workspace's templates are configuration
        somebody maintains by hand rather than a list that grows with use, so
        the whole set comes back under a ceiling. See
        `app.services.templates.TEMPLATES_MAX` for that ceiling and for what
        would have to change if a workspace ever reached it.

        A team id from another workspace is not refused and is not reported: it
        selects no team-scoped rows, so the answer is the shared templates --
        the same answer a real team with none of its own gets. Reporting
        otherwise would confirm that another tenant's team exists.
        """
        scope = await authorized_scope(info, workspace_slug)

        templates = await info.context.template_service.list(
            scope=scope,
            team_id=team_id,
        )

        return [IssueTemplateType.from_entity(entity) for entity in templates]

    @strawberry.field
    async def issue_template(
        self,
        info: Info,
        workspace_slug: str,
        id: UUID,
    ) -> IssueTemplateType | None:
        """One template from this workspace, or null.

        Null covers both "no such template" and "a template in another
        workspace", and the two must not be distinguishable: an error, or any
        other different answer, would tell a caller holding a guessed id that
        the template is real and simply not theirs.
        """
        scope = await authorized_scope(info, workspace_slug)

        entity = await info.context.template_service.get(scope=scope, template_id=id)

        if entity is None:
            return None

        return IssueTemplateType.from_entity(entity)
