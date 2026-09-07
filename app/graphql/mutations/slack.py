import strawberry
from strawberry.types import Info

from app.domain.errors import ValidationError
from app.graphql.inputs.slack import (
    SlackChannelsSyncInput,
    SlackDefaultChannelSetInput,
    SlackDisconnectInput,
    SlackNotificationPreferenceSetInput,
    SlackTestNotificationInput,
)
from app.graphql.queries.slack import slack_admin_scope
from app.graphql.types.errors import ValidationErrorType
from app.graphql.types.slack import (
    SlackChannelsSyncPayload,
    SlackDisconnectPayload,
    SlackIntegrationType,
    SlackNotificationSettingsPayload,
    SlackNotificationSettingsType,
    SlackTestNotificationPayload,
)


def _errors(exc: ValidationError) -> list[ValidationErrorType]:
    return [ValidationErrorType.from_domain(issue) for issue in exc.issues]


@strawberry.type
class SlackMutation:
    """Slack writes, composed into the root Mutation by app/graphql/schema.py.

    Connecting is not here and cannot be: an OAuth grant is a browser redirect
    to Slack and back, which is a REST flow by nature (see app/rest/slack.py).
    Everything else about the integration is an ordinary authorized write with
    no browser in the loop, so it belongs on the product API.

    Three of these five reach Slack over the network -- the sync and the test
    notification directly, and nothing else. Neither reports success it did not
    observe: the sync says which channels are stale and why, and the test says
    whether Slack acknowledged the message.

    `slack_admin_scope` is imported from the query module rather than
    duplicated. The authorization for reading the integration and for changing
    it is the same question, and it should have exactly one answer.
    """

    @strawberry.mutation
    async def slack_disconnect(
        self,
        info: Info,
        input: SlackDisconnectInput,
    ) -> SlackDisconnectPayload:
        """Remove this workspace's Slack installation.

        Idempotent: disconnecting a workspace that was not connected succeeds
        and reports DISCONNECTED. Two admins pressing the same button is an
        ordinary race, and an error on the second would describe the first
        admin's action rather than anything the second did wrong.

        This also discards the workspace's channel list, its chosen channel and
        its notification preferences, in one transaction with the installation
        -- see SlackService.disconnect. Reconnecting starts from an empty
        picker, which is the truthful state: the channels were what one bot
        token could see, and that token is gone.

        The token is deleted here and NOT revoked at Slack; SlackService
        .disconnect says exactly what that leaves live and why revocation
        belongs with a retryable job rather than in this request.
        """
        scope = await slack_admin_scope(info, input.workspace_slug)

        view = await info.context.slack_service.disconnect(scope=scope)

        return SlackDisconnectPayload(
            integration=SlackIntegrationType.from_view(view),
        )

    @strawberry.mutation
    async def slack_channels_sync(
        self,
        info: Info,
        input: SlackChannelsSyncInput,
    ) -> SlackChannelsSyncPayload:
        """Ask Slack which channels exist, and record the answer.

        A mutation rather than a query because it writes, and because the
        network call belongs on a path a client chose to take. A settings page
        renders from `slackChannels`; this is the refresh button beside it.

        A failure is reported in the payload rather than raised. Every way this
        can fail -- Slack unreachable, a declined scope, a revoked token -- is
        an ordinary state of a third-party integration that an admin can act
        on, not a defect, and a GraphQL error would be masked into "Internal
        server error" and tell them nothing.

        The channels come back either way; on a failure they are the cached
        ones. See SlackChannelsSyncPayload for why that pairing matters.
        """
        scope = await slack_admin_scope(info, input.workspace_slug)

        sync = await info.context.slack_service.sync_channels(scope=scope)

        return SlackChannelsSyncPayload.from_domain(sync)

    @strawberry.mutation
    async def slack_default_channel_set(
        self,
        info: Info,
        input: SlackDefaultChannelSetInput,
    ) -> SlackNotificationSettingsPayload:
        """Choose the channel this workspace's notifications go to.

        The channel id is resolved against this workspace's own cached
        channels, so one belonging to another tenant answers exactly as one
        that does not exist -- a field error, indistinguishable from a typo.
        The stored display name is read from that row and never from the
        request.

        Answers with the whole settings record, not just the channel, so a
        client re-renders one screen from one source.
        """
        scope = await slack_admin_scope(info, input.workspace_slug)

        try:
            settings = await info.context.slack_service.set_default_channel(
                scope=scope,
                channel_id=input.channel_id,
            )
        except ValidationError as exc:
            # Only expected input validation reaches the payload. Everything
            # else propagates through GraphQL's error mechanism and is masked.
            return SlackNotificationSettingsPayload(
                settings=None,
                errors=_errors(exc),
            )

        return SlackNotificationSettingsPayload(
            settings=SlackNotificationSettingsType.from_entity(settings),
            errors=[],
        )

    @strawberry.mutation
    async def slack_notification_preference_set(
        self,
        info: Info,
        input: SlackNotificationPreferenceSetInput,
    ) -> SlackNotificationSettingsPayload:
        """Turn one event's Slack announcement on or off.

        One event per call, so two admins toggling two switches do not
        overwrite each other -- which a mutation taking the whole set would.

        `input.event` is an enum member, so `.value` is the domain string the
        service and the database vocabulary are both written in. The service
        checks it again: this resolver is one transport, and the rule is not
        the transport's.
        """
        scope = await slack_admin_scope(info, input.workspace_slug)

        try:
            settings = await info.context.slack_service.set_notification_preference(
                scope=scope,
                event=input.event.value,
                enabled=input.enabled,
            )
        except ValidationError as exc:
            return SlackNotificationSettingsPayload(
                settings=None,
                errors=_errors(exc),
            )

        return SlackNotificationSettingsPayload(
            settings=SlackNotificationSettingsType.from_entity(settings),
            errors=[],
        )

    @strawberry.mutation
    async def slack_test_notification(
        self,
        info: Info,
        input: SlackTestNotificationInput,
    ) -> SlackTestNotificationPayload:
        """Post a fixed message to the configured channel, and say what happened.

        `delivered` is true only when Slack acknowledged the message. An admin
        presses this because they do not yet believe the integration works, so
        the one thing it must never do is report a success it did not observe
        -- which is why the service has no branch that catches an exception and
        answers true.

        The commonest real failure is CHANNEL_UNAVAILABLE from Slack's
        `not_in_channel`: Vector holds `chat:write`, which permits posting to
        channels it has been invited to. The fix is `/invite` in Slack, and
        `SlackChannel.isMember` is what lets a client say so.
        """
        scope = await slack_admin_scope(info, input.workspace_slug)

        result = await info.context.slack_service.send_test_notification(scope=scope)

        return SlackTestNotificationPayload.from_domain(result)
