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
