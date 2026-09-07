import strawberry
from graphql import GraphQLError
from strawberry.types import Info

from app.domain.errors import WorkspaceAccessDeniedError
from app.domain.tenancy import AuthorizedWorkspaceScope
from app.graphql.queries.memberships import WORKSPACE_NOT_FOUND_MESSAGE
from app.graphql.types.slack import (
    SlackChannelType,
    SlackIntegrationType,
    SlackNotificationSettingsType,
)
from app.graphql.viewer import viewer_user_id


async def slack_admin_scope(info: Info, slug: str) -> AuthorizedWorkspaceScope:
    """Resolve the viewer to an admin of this workspace, or NOT_FOUND.

    The authorization step both Slack resolvers share, in one place because
    two copies of an authorization check are two things that can drift and the
    one that drifts is the one nobody re-read.

    Three refusals collapse into one answer, and the collapse is the point:

    * the slug names no workspace,
    * it names one the viewer is not a member of,
    * it names one the viewer is in but may not administer.

    The first two are already indistinguishable by construction --
    `MembershipRepository.find_membership` answers both with the same absent
    row from one statement, so the server never computes the difference. The
    third is folded in here deliberately: to a member who may not administer
    the workspace, the integration does not exist. Answering "forbidden"
    instead would tell every member of every workspace that this deployment
    has a Slack app configured and that admins can reach it.

    `viewer_user_id` is called first and raises before any service is touched,
    so an unauthenticated request performs no protected lookup at all.
    """
    user_id = await viewer_user_id(info)

    try:
        scope = await info.context.membership_service.authorized_scope_for_slug(
            slug=slug,
            user_id=user_id,
        )

        # Raises the same WorkspaceAccessDeniedError as the lookup above, from
        # the service that owns the rule. Inside the same `try` so both
        # refusals leave by the same path -- and so a reader cannot conclude
        # from the structure that the two are told apart anywhere.
        info.context.slack_service.require_admin(scope)
    except WorkspaceAccessDeniedError:
        # Only this one expected refusal is translated. Anything else -- an
        # asyncpg failure, a bug -- propagates and is masked rather than being
        # reported to the client as a missing workspace. `from None` keeps the
        # domain exception out of the response.
        raise GraphQLError(
            WORKSPACE_NOT_FOUND_MESSAGE,
            extensions={"code": "NOT_FOUND"},
        ) from None

    # Annotated rather than returned inline: the context attribute is untyped
    # here, so returning it directly would satisfy any return type.
    authorized: AuthorizedWorkspaceScope = scope

    return authorized


@strawberry.type
class SlackQuery:
    """Root query fields for the Slack integration.

    A class of its own, merged into the schema's root `Query` in
    app.graphql.schema. Every field here is scoped to the viewer: the workspace
    slug selects what is being asked about and never who is asking, and all
    three go through `slack_admin_scope` before touching anything.

    None of them calls Slack. Refreshing the channel list is
    `slackChannelsSync`, a mutation, because it writes -- and because a query
    that reached a third party would make a settings page's render time depend
    on Slack's, and would spend a workspace's rate limit on people looking at a
    page.
    """

    @strawberry.field
    async def slack_integration(
        self,
        info: Info,
        workspace_slug: str,
    ) -> SlackIntegrationType:
        """This workspace's Slack integration, for an admin of it.

        Non-null, and it answers for a workspace with no installation rather
        than returning null. "Not connected" is a real state with a real
        screen behind it -- that is the whole reason DISCONNECTED exists as a
        status -- so a null here would make the client infer from an absence
        the thing the field is for saying out loud.

        A deployment with no Slack app answers UNCONFIGURED without reaching
        the database; see SlackService.status.
        """
        scope = await slack_admin_scope(info, workspace_slug)

        view = await info.context.slack_service.status(scope=scope)

        return SlackIntegrationType.from_view(view)

    @strawberry.field
    async def slack_channels(
        self,
        info: Info,
        workspace_slug: str,
    ) -> list[SlackChannelType]:
        """The channels this workspace last saw, for an admin of it.

        A cache read, not a call to Slack. An empty list is the honest answer
        for a workspace that has connected but never refreshed, and for one
        that is not connected at all -- the two are told apart by
        `slackIntegration.status`, which is the field whose job that is.

        Inaccessible channels are included, flagged. A picker filters them; a
        settings screen explaining why notifications stopped needs them.
        """
        scope = await slack_admin_scope(info, workspace_slug)

        channels = await info.context.slack_service.channels(scope=scope)

        return [SlackChannelType.from_entity(channel) for channel in channels]

    @strawberry.field
    async def slack_notification_settings(
        self,
        info: Info,
        workspace_slug: str,
    ) -> SlackNotificationSettingsType:
        """Where this workspace posts and what it announces, for an admin.

        Non-null, and it answers for a workspace that has configured nothing
        rather than returning null -- for the reason `slackIntegration` is
        non-null. "No channel chosen, everything off" is a real state with a
        real screen behind it, and a null here would make a client infer it
        from an absence.
        """
        scope = await slack_admin_scope(info, workspace_slug)

        settings = await info.context.slack_service.notification_settings(scope=scope)

        return SlackNotificationSettingsType.from_entity(settings)
