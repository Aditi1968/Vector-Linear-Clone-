import strawberry

from app.graphql.types.slack import SlackNotificationEventType


@strawberry.input
class SlackDisconnectInput:
    """Which workspace's Slack integration to remove.

    A slug and nothing else. In particular there is no installation id and no
    Slack team id: a workspace has one installation, so naming it a second way
    would be an argument a client could get wrong -- and an id argument is the
    shape that lets a caller name a row belonging to someone else's tenant and
    find out whether it exists.

    An input type for one field, matching every other mutation here. The
    argument is `input:` so that a second field can be added later without
    changing the mutation's signature, which is the whole convention.
    """

    workspace_slug: str


@strawberry.input
class SlackChannelsSyncInput:
    """Which workspace's channel list to refresh from Slack.

    A slug and nothing else. In particular there is no cursor, no page size and
    no channel-type selector: what to ask Slack for is decided in
    app/services/slack.py, next to the granted scope that decides what may be
    asked at all. A `types` argument here would be a way for a client to
    request private channels and turn the whole call into a `missing_scope`
    refusal.
    """

    workspace_slug: str


@strawberry.input
class SlackDefaultChannelSetInput:
    """Which channel this workspace's notifications should go to.

    `channel_id` is Slack's id and is attacker-controlled: it arrives from a
    browser, and a picker is a suggestion rather than a constraint. It is
    resolved against `slack_channels` scoped to the caller's own workspace, and
    the display name is read from that row -- there is deliberately no
    `channel_name` field here, because a name a client supplied is a name
    nothing can check and the stored copy is what a settings screen shows.

    No field for clearing the choice. Removing a default channel is
    disconnecting or choosing another one; an explicit "post nowhere" state
    would be a third thing to render and nobody has asked for it.
    """

    workspace_slug: str
    channel_id: str


@strawberry.input
class SlackNotificationPreferenceSetInput:
    """Turn one event's Slack announcement on or off.

    One event per call rather than a list of them. Two admins on the settings
    screen at once each toggle one switch, and a mutation that took the whole
    set would have the second write silently revert the first's -- the
    read-modify-write this schema keeps out of the product by making the
    preference a row per event.

    `enabled` is required and has no default. A toggle whose "off" is a missing
    field is a toggle that cannot be turned off by a client that omits it.
    """

    workspace_slug: str
    event: SlackNotificationEventType
    enabled: bool


@strawberry.input
class SlackTestNotificationInput:
    """Send the test message to this workspace's default channel.

    No channel and no message text. Both are the point: the channel is the one
    that is actually configured, so the test exercises the real path rather
    than one an argument could steer, and the text is a fixed string in
    app/services/slack.py -- a `text` field here would be an authenticated way
    to make Vector say arbitrary words in a company's Slack.
    """

    workspace_slug: str
