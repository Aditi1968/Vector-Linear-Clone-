"""Slack, as the application models it: an installation and a status.

Pure application code -- no Strawberry, FastAPI, asyncpg or PostgreSQL. The
one thing worth saying up front is what is deliberately absent: neither type
here carries a bot token. See SlackInstallationEntity.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Final
from uuid import UUID


# Every status `slackIntegration` can report, and the vocabulary the GraphQL
# enum copies. Three, because the two failure-ish states have different
# audiences and different fixes:
#
# * UNCONFIGURED -- this deployment has no Slack app. Nobody in the product
#   can fix it; an operator sets SLACK_CLIENT_ID and friends. Every workspace
#   in the deployment reads this, regardless of what the database holds.
# * DISCONNECTED -- the deployment can talk to Slack, this workspace has not
#   connected. An admin fixes it with one button.
# * CONNECTED -- there is an installation row.
#
# Collapsing the first two into one "not connected" is the mistake this
# vocabulary exists to prevent: it shows an admin a connect button that
# cannot work, and shows an operator nothing at all.
#
# Pinned equal to the GraphQL enum by tests/test_slack_graphql.py, the way
# WORKSPACE_ROLES is pinned to WorkspaceRoleType.
SLACK_STATUSES: Final = ("unconfigured", "disconnected", "connected")

# The roles that may see or change a workspace's Slack integration.
#
# A subset of app.domain.tenancy.WORKSPACE_ROLES, and not a privilege
# comparison: WORKSPACE_ROLES is documented as carrying no ordering, so
# membership of this set is spelled out rather than derived from a rank. An
# integration holds a credential that can post as the company into its own
# Slack, which is not a thing every member of a workspace gets to hand out or
# take away.
SLACK_ADMIN_ROLES: Final = frozenset({"admin", "owner"})

# The events a workspace may have announced in Slack, and the application's
# copy of `slack_notification_preferences_event_known` in migration 018.
#
# A tuple rather than a set, because the order is the order a settings screen
# lists them in and an unordered vocabulary would let that order drift between
# releases for no reason a reader could see.
#
# Six, and short on purpose. Everything that happens to an issue belongs in its
# history; almost none of it belongs in a channel a whole team reads, and a
# vocabulary mirroring `ActivityKind` would produce a channel muted within a
# week -- which is the same as no integration, arrived at expensively. The same
# argument `NotificationKind` makes about the inbox.
#
# Pinned equal to the GraphQL enum by tests/test_slack_channels.py, the way
# SLACK_STATUSES is pinned to SlackIntegrationStatusType.
SLACK_NOTIFICATION_EVENTS: Final = (
    "issue_assigned",
    "issue_completed",
    "issue_priority_urgent",
    "project_health_changed",
    "project_update_published",
    "pull_request_merged",
)

# The Slack scope each product action needs, as Slack names it.
#
# Read against `slack_installations.scopes` -- what Slack actually granted --
# and never against REQUESTED_SCOPES, for the reason migration 014 states on
# that column: an admin can decline scopes individually, so what this release
# asks for says nothing about what this workspace permits.
SCOPE_LIST_CHANNELS: Final = "channels:read"
SCOPE_POST_MESSAGE: Final = "chat:write"

# Why a Slack call did not happen, or did not work.
#
# A closed vocabulary of OUR words, deliberately not Slack's error strings.
# Slack's `error` field is a third party's identifier set: it changes without
# notice, it is not documented as stable, and echoing it would put a provider's
# vocabulary in our schema and in a UI's switch statement. Each member below is
# a different sentence to an admin and a different thing to do next, which is
# the only test a member has to pass:
#
# * NOT_CONNECTED       -- there is no installation. Connect Slack.
# * MISSING_SCOPE       -- the grant does not carry the scope this needs,
#                          because an admin declined it. Reconnect.
# * NO_DEFAULT_CHANNEL  -- connected, but nobody has chosen where to post.
# * CHANNEL_UNAVAILABLE -- the chosen channel is archived, gone, or the bot is
#                          not in it. Invite the bot, or choose another.
# * SLACK_REFUSED       -- Slack answered and said no, for a reason none of the
#                          above covers. An operator reads the log.
# * SLACK_UNREACHABLE   -- Slack did not answer at all. Try again.
#
# There is no SUCCESS member: success is the absence of a failure, and a
# vocabulary that can spell "succeeded" is one a caller can report success
# with while holding an exception.
SLACK_FAILURES: Final = (
    "not_connected",
    "missing_scope",
    "no_default_channel",
    "channel_unavailable",
    "slack_refused",
    "slack_unreachable",
)

# Slack's own error codes that mean "that channel is not one you can post to".
#
# Read at exactly one place -- SlackService, translating a refusal into
# SLACK_FAILURES -- so this is the only frame in the codebase that knows
# Slack's spelling. A code outside this set is SLACK_REFUSED, which is the
# honest answer for a refusal this version has not been taught to explain.
CHANNEL_UNAVAILABLE_CODES: Final = frozenset(
    {
        "channel_not_found",
        "not_in_channel",
        "is_archived",
        "restricted_action",
    }
)


class SlackOAuthError(Exception):
    """Slack did not answer an OAuth exchange with a usable grant.

    Covers a refused code, an expired one, a code already redeemed, and a
    response whose shape this version does not understand. One exception for
    all of them because the caller's response is the same in every case --
    send the admin back to try again -- and because the distinctions Slack
    draws here are its own and change without notice.

    In app/domain/slack.py rather than app/domain/errors.py, which holds the
    vocabulary shared across features. This one is not shared: it names a
    third party failing, not user input being wrong, and nothing outside the
    Slack code should be catching it.

    Carries no detail, for the reason WorkspaceNotFoundError gives: whoever
    raises it holds the response and can log what is safe to log, in the frame
    that knows the difference. Slack's error responses can echo the code that
    was submitted, and a code is a credential.
    """

    def __init__(self) -> None:
        super().__init__("Slack OAuth exchange failed")


class SlackTeamAlreadyConnectedError(Exception):
    """This Slack workspace is already connected to another Vector workspace.

    Raised from `slack_installations_team_key`, because "already connected" is
    a fact only the database holds and only its unique constraint can decide
    without a window between the check and the insert.

    A distinct error rather than a generic failure because it is the one
    outcome an admin can act on -- somebody else in the company connected the
    same Slack first -- and because it is expected: a company with two Vector
    workspaces will hit it on the second connect, not on a broken one.

    Names neither workspace. Telling the admin *which* Vector workspace holds
    the connection would answer, for anyone who can reach a connect button,
    which workspaces exist in this deployment and who is in them.
    """

    def __init__(self) -> None:
        super().__init__("Slack workspace is already connected")


@dataclass(frozen=True, slots=True)
class SlackInstallationEntity:
    """One workspace's connection to one Slack workspace.

    Carries no bot token, and that is structural rather than an oversight.
    This object is what services return and what the GraphQL layer maps into a
    response type, so a token field here would be one careless `from_entity`
    away from a credential in an API response or a log line. The token is
    reached only through SlackTokenStore, by a caller that asked for it by
    name; see app/services/slack.py.

    `scopes` is a tuple rather than a list so the whole entity is hashable and
    cannot be mutated by a caller that got a reference to it -- the same
    reason every dataclass here is frozen.
    """

    workspace_id: UUID
    slack_team_id: str
    slack_team_name: str
    bot_user_id: str
    scopes: tuple[str, ...]
    connected_by_user_id: UUID
    connected_at: datetime


@dataclass(frozen=True, slots=True)
class SlackIntegrationView:
    """What a client is told about a workspace's Slack integration.

    A separate type from the entity, because the two answer different
    questions. The entity describes a row that exists; this describes a
    workspace, which usually has no row -- so `status` is always present and
    everything else is absent unless CONNECTED.

    Nothing here is a secret: a status, a display name and the scope list.
    That is the contract, and it is enforced by this type having no other
    fields rather than by every resolver remembering not to select them.
    """

    status: str
    team_name: str | None
    scopes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SlackGrant:
    """What an OAuth exchange yields, before anything is stored.

    The one type here that does hold the bot token, because there is a moment
    -- between Slack answering `oauth.v2.access` and the token store taking
    custody -- when the process has it and nothing has yet decided where it
    goes. Keeping that moment in its own type is what lets every other type in
    this module be token-free by construction.

    It never reaches a repository, a GraphQL type or a log line: SlackService
    unpacks it, hands the token to the store, and writes the reference.
    """

    slack_team_id: str
    slack_team_name: str
    bot_user_id: str
    scopes: tuple[str, ...]

    # `repr=False`, so the one field in this module that holds a credential
    # cannot be printed by the machinery that prints objects without being
    # asked: a dataclass repr in a traceback, an error reporter that captures
    # locals, a `print` while debugging the callback. Reading it still works;
    # only the accidental paths are closed.
    bot_token: str = field(repr=False)


class SlackApiError(Exception):
    """A Slack Web API call did not do what it was asked.

    Carries `code`, which is Slack's own `error` string when Slack answered
    with one and None when it did not answer at all (a timeout, a refused
    connection, a body that is not JSON). Those two cases are genuinely
    different -- one means "Slack said no", the other "we never found out" --
    and collapsing them would have the product tell an admin their channel is
    wrong when the truth is that Slack is down.

    The code is deliberately NOT for showing to anybody. It exists so that
    SlackService can translate it once, into the closed SLACK_FAILURES
    vocabulary above, and so that an operator log can carry the provider's own
    word for what happened. Nothing in the GraphQL layer reads it.

    In app/domain/slack.py beside SlackOAuthError rather than in
    app/domain/errors.py, for the same reason that one gives: this names a
    third party failing, not user input being wrong.
    """

    def __init__(self, code: str | None = None) -> None:
        super().__init__("Slack API call failed")

        self.code = code


@dataclass(frozen=True, slots=True)
class SlackChannelEntity:
    """One channel `conversations.list` reported, as this workspace sees it.

    Carries no workspace id. Every read of these is already scoped to one
    workspace by the statement that produced it, and a tenant id on a type the
    GraphQL layer maps from is a tenant id one careless `from_entity` away from
    a response field -- which is a way for a client to learn ids it should be
    unable to name.

    `is_member` is the field a product decision hangs on rather than a
    curiosity: with only `chat:write` granted, posting into a public channel
    the bot has not joined is refused, and this is what lets a picker say
    "invite the bot first" instead of letting an admin choose a channel that
    cannot work.
    """

    channel_id: str
    name: str
    is_private: bool
    is_archived: bool
    is_member: bool
    is_accessible: bool


@dataclass(frozen=True, slots=True)
class SlackNotificationSettingsEntity:
    """Where a workspace's Slack notifications go, and which ones are on.

    `default_channel_id` and `default_channel_name` are absent together or
    present together -- `slack_notification_settings_channel_pair` in 018 is
    what makes the third and fourth combinations unstorable, so no reader here
    has to handle them.

    `preferences` is every event in SLACK_NOTIFICATION_EVENTS, always, with the
    stored value where there is one and False where there is not. Merged in the
    service rather than left to each caller, so that a client cannot render a
    settings screen missing a toggle for an event nobody has touched yet.
    """

    default_channel_id: str | None
    default_channel_name: str | None
    preferences: tuple["SlackNotificationPreference", ...]


@dataclass(frozen=True, slots=True)
class SlackNotificationPreference:
    """One event, and whether this workspace wants it announced."""

    event: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class SlackChannelSync:
    """What a channel refresh produced: the channels, or why there are none.

    Both fields are always present and exactly one is meaningful, which is the
    shape that makes a partial answer impossible to report as a whole one. A
    failed sync answers with `failure` set and the channels the CACHE still
    holds -- not an empty list -- because a settings screen that blanked its
    picker every time Slack was slow would look like a workspace that lost its
    channels.
    """

    channels: tuple[SlackChannelEntity, ...]
    failure: str | None


@dataclass(frozen=True, slots=True)
class SlackDeliveryResult:
    """Whether a message actually reached Slack.

    `delivered` is set from a Slack response that said `ok: true` and from
    nothing else. There is no path in this feature where an exception is
    caught, logged and reported as a success -- which is the failure mode a
    test-notification button exists to rule out, since an admin presses it
    precisely because they do not yet believe the integration works.
    """

    delivered: bool
    failure: str | None
