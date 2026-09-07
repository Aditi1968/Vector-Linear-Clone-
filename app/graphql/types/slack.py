from enum import Enum

import strawberry

from app.domain.slack import (
    SlackChannelEntity,
    SlackChannelSync,
    SlackDeliveryResult,
    SlackIntegrationView,
    SlackNotificationPreference,
    SlackNotificationSettingsEntity,
)
from app.graphql.types.errors import ValidationErrorType


@strawberry.enum(name="SlackIntegrationStatus")
class SlackIntegrationStatusType(Enum):
    """The transport's copy of app.domain.slack.SLACK_STATUSES.

    An enum here and a str in the domain, for the reason WorkspaceRoleType
    gives: a closed set is what a client wants -- generated types, exhaustive
    switches, no string literals in a UI -- while the value's authority is the
    server's rather than this declaration's.

    The three members exist because a UI has three different screens to show,
    and the distinction this enum is really here to preserve is the first two.
    UNCONFIGURED means the deployment has no Slack app: the honest UI is "your
    administrator has not set this up", with no button, because pressing one
    could not work. DISCONNECTED means it does, and this workspace has not
    connected: a connect button, and it will work. Reporting the first as the
    second is the lie this integration must not tell.

    The three copies (this enum, SLACK_STATUSES, the strings SlackService
    builds views with) are pinned equal by tests/test_slack_graphql.py.
    """

    UNCONFIGURED = "unconfigured"
    DISCONNECTED = "disconnected"
    CONNECTED = "connected"


@strawberry.type(name="SlackIntegration")
class SlackIntegrationType:
    """A workspace's Slack integration, as an admin sees it.

    Three fields, and the list of what is deliberately absent is longer than
    the list of what is here: no bot token, no token reference, no signing
    secret, no client secret, no client id. None of them is omitted by
    oversight and none may be added -- a bot token in a GraphQL field is a
    credential handed to every browser that renders a settings page, and to
    every log, cache and error reporter between here and there. The type
    cannot carry one because it has no field for one, which is a stronger
    guarantee than a resolver remembering not to select it.

    `teamName` and `scopes` are empty unless CONNECTED, which the domain view
    guarantees in one place rather than each resolver deciding.

    No `connectedAt` and no `connectedBy`, though the database records both.
    They are an audit question rather than a settings-screen one, and this
    type is what a settings screen reads; the day there is an audit surface,
    it can expose them under its own authorization rather than widening this.
    """

    status: SlackIntegrationStatusType
    team_name: str | None
    scopes: list[str]

    @classmethod
    def from_view(cls, view: SlackIntegrationView) -> "SlackIntegrationType":
        """Build the transport type, raising on a status this schema cannot say.

        `SlackIntegrationStatusType(view.status)` raises ValueError for a
        status the enum does not carry, which reaches the client as a masked
        internal error and the logs as a real traceback. That is the intended
        outcome, for the reason WorkspaceMembershipType.from_entity gives: a
        fallback would report a state the server did not compute, and the
        plausible fallback here -- DISCONNECTED -- is precisely the one that
        would show a connect button for a deployment that has no Slack app.
        """
        return cls(
            status=SlackIntegrationStatusType(view.status),
            team_name=view.team_name,
            # A list because GraphQL has no tuple; the domain holds a tuple so
            # that nothing can append to a workspace's granted scopes.
            scopes=list(view.scopes),
        )


@strawberry.type
class SlackDisconnectPayload:
    """What `slackDisconnect` answers with: the integration as it now stands.

    No `errors` list, unlike the cycle and project payloads, and the reason is
    that this mutation has no input a client can get wrong. Its only argument
    is a workspace slug, and a slug that names nothing -- or a workspace the
    caller may not administer -- is answered with a NOT_FOUND error rather
    than a field error, because the client cannot fix it by editing a form. An
    empty `errors` list on every response would be a field clients learn to
    ignore.

    The resulting integration is returned rather than a bare boolean, so a
    client re-renders from what the server says rather than from what it
    assumes a successful disconnect implies. On an unconfigured deployment
    that answer is UNCONFIGURED, not DISCONNECTED, which is the distinction
    this whole feature is careful about.
    """

    integration: SlackIntegrationType


@strawberry.enum(name="SlackNotificationEvent")
class SlackNotificationEventType(Enum):
    """The transport's copy of app.domain.slack.SLACK_NOTIFICATION_EVENTS.

    An enum here and a str in the domain, for the reason
    SlackIntegrationStatusType gives: a closed set is what a client wants, and
    the value's authority is the server's.

    It also does a job on the way IN, which the status enum does not: this is
    the type of the `event` argument on `slackNotificationPreferenceSet`, so a
    misspelled event is a document that does not validate rather than a row
    that never matches anything. The service checks the vocabulary again,
    because the enum guards one transport and the rule is not the transport's.

    Pinned equal to the domain tuple by tests/test_slack_channels.py.
    """

    ISSUE_ASSIGNED = "issue_assigned"
    ISSUE_COMPLETED = "issue_completed"
    ISSUE_PRIORITY_URGENT = "issue_priority_urgent"
    PROJECT_HEALTH_CHANGED = "project_health_changed"
    PROJECT_UPDATE_PUBLISHED = "project_update_published"
    PULL_REQUEST_MERGED = "pull_request_merged"


@strawberry.enum(name="SlackFailure")
class SlackFailureType(Enum):
    """Why a Slack call did not happen, or did not work.

    Vector's vocabulary, not Slack's. Slack's `error` strings are a third
    party's identifier set that changes without notice, and echoing one would
    put a provider's spelling into this schema and into a client's switch
    statement. Each member here is a different sentence to an admin and a
    different next step; see app.domain.slack.SLACK_FAILURES, which this is the
    transport's copy of.

    There is deliberately no member meaning success. Success is the absence of
    a failure -- `SlackTestNotificationPayload.delivered` is the boolean, and
    this field is null beside it -- so there is no way to spell a delivery that
    both worked and reports a reason.
    """

    NOT_CONNECTED = "not_connected"
    MISSING_SCOPE = "missing_scope"
    NO_DEFAULT_CHANNEL = "no_default_channel"
    CHANNEL_UNAVAILABLE = "channel_unavailable"
    SLACK_REFUSED = "slack_refused"
    SLACK_UNREACHABLE = "slack_unreachable"


@strawberry.type(name="SlackChannel")
class SlackChannelType:
    """One Slack channel, as this workspace last saw it.

    `id` is Slack's channel id and is what a client sends back to choose a
    default. It is not a Vector identifier and is not a global one: it is
    meaningful only inside the Slack workspace this Vector workspace is
    connected to, and every server-side lookup of it is scoped to the caller's
    own workspace.

    `isMember` and `isAccessible` are here because the product has to say
    something specific when posting fails, and "something went wrong" is the
    answer that leaves an admin with nothing to do:

    * `isMember` false means Vector is not in the channel. With the
      `chat:write` scope this integration holds, posting there is refused --
      the fix is `/invite` in Slack, which is a sentence the UI can only offer
      if it knows.
    * `isAccessible` false means the last refresh no longer listed the channel:
      deleted, made private, or out of the bot's view. The row survives so the
      name can still be shown next to the explanation.

    No `workspaceId` and no Slack team id. Neither is anything a client needs
    and both are identifiers a client should not be handed for free.
    """

    id: str
    name: str
    is_private: bool
    is_archived: bool
    is_member: bool
    is_accessible: bool

    @classmethod
    def from_entity(cls, entity: SlackChannelEntity) -> "SlackChannelType":
        return cls(
            id=entity.channel_id,
            name=entity.name,
            is_private=entity.is_private,
            is_archived=entity.is_archived,
            is_member=entity.is_member,
            is_accessible=entity.is_accessible,
        )


@strawberry.type(name="SlackNotificationPreference")
class SlackNotificationPreferenceType:
    """One event and whether this workspace wants it announced.

    Always present for every event in the vocabulary, whether or not a row has
    ever been written -- the service merges the stored rows with the full list.
    A client rendering from the stored rows alone would show a settings screen
    with no toggles at all on the day Slack is connected.
    """

    event: SlackNotificationEventType
    enabled: bool

    @classmethod
    def from_entity(
        cls,
        entity: SlackNotificationPreference,
    ) -> "SlackNotificationPreferenceType":
        return cls(
            event=SlackNotificationEventType(entity.event),
            enabled=entity.enabled,
        )


@strawberry.type(name="SlackNotificationSettings")
class SlackNotificationSettingsType:
    """Where a workspace posts, and what it posts.

    `defaultChannelId` and `defaultChannelName` are null together or set
    together; migration 018 makes the other two combinations unstorable, so a
    client may branch on either one.

    The name is the denormalised copy rather than a join, which is what lets
    this answer for a channel the workspace can no longer see -- exactly the
    case where a settings screen most needs a name to put in its explanation.
    """

    default_channel_id: str | None
    default_channel_name: str | None
    preferences: list[SlackNotificationPreferenceType]

    @classmethod
    def from_entity(
        cls,
        entity: SlackNotificationSettingsEntity,
    ) -> "SlackNotificationSettingsType":
        return cls(
            default_channel_id=entity.default_channel_id,
            default_channel_name=entity.default_channel_name,
            preferences=[
                SlackNotificationPreferenceType.from_entity(preference)
                for preference in entity.preferences
            ],
        )


@strawberry.type
class SlackChannelsSyncPayload:
    """What `slackChannelsSync` answers with: the list, and whether it is new.

    `failure` is null when the refresh actually reached Slack, and `channels`
    is then what Slack reported. When it is set, `channels` is what the CACHE
    still holds -- not an empty list -- so a client re-renders a picker that
    still works rather than one that has apparently lost every channel because
    Slack was slow for three seconds.

    That pairing is the whole reason this payload has two fields instead of
    being a plain list. A list alone cannot say "this is stale", and a client
    that cannot tell has no way to avoid reporting a successful refresh that
    did not happen.
    """

    channels: list[SlackChannelType]
    failure: SlackFailureType | None

    @classmethod
    def from_domain(cls, sync: SlackChannelSync) -> "SlackChannelsSyncPayload":
        return cls(
            channels=[
                SlackChannelType.from_entity(channel) for channel in sync.channels
            ],
            failure=None if sync.failure is None else SlackFailureType(sync.failure),
        )


@strawberry.type
class SlackNotificationSettingsPayload:
    """What both settings mutations answer with.

    One payload for `slackDefaultChannelSet` and
    `slackNotificationPreferenceSet` rather than one each, because both write
    part of one record and both must answer with the whole of it: a client that
    got back only the field it changed would re-render a settings screen from
    two sources and show a stale one beside a fresh one.

    `settings` is null exactly when `errors` is non-empty. Unlike
    SlackDisconnectPayload these mutations DO have input a client can get wrong
    -- a channel that has gone, an event this release retired -- so they carry
    the structured errors list every other write in this schema uses.
    """

    settings: SlackNotificationSettingsType | None
    errors: list[ValidationErrorType]


@strawberry.type
class SlackTestNotificationPayload:
    """Whether the test message actually reached Slack.

    `delivered` is true only for a Slack response that said so. It is not a
    default, it is not set optimistically before the call, and no branch in
    this feature catches an exception and reports success -- which is the
    failure this button exists to rule out, since an admin presses it precisely
    because they do not yet believe the integration works.

    `failure` is null when `delivered` is true and set otherwise, so a client
    never has to infer a reason from a bare false.
    """

    delivered: bool
    failure: SlackFailureType | None

    @classmethod
    def from_domain(
        cls,
        result: SlackDeliveryResult,
    ) -> "SlackTestNotificationPayload":
        return cls(
            delivered=result.delivered,
            failure=(
                None if result.failure is None else SlackFailureType(result.failure)
            ),
        )
