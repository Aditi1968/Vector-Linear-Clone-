from enum import Enum

import strawberry

from app.domain.slack import SlackIntegrationView


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
