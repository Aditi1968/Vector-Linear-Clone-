"""The Slack GraphQL surface, and the rules behind it. No database involved.

The schema is the real one, built through `build_schema`, so the masking
policy in `app.graphql.schema` applies exactly as in production: an error code
outside PUBLIC_ERROR_CODES arrives with its message replaced, and these tests
fail if NOT_FOUND or UNAUTHENTICATED is ever unpublished.

Three things are asked of this layer:

  * no token, secret or credential can be selected through it;
  * an admin of workspace A cannot read or change workspace B's integration,
    and cannot learn that B exists by trying;
  * UNCONFIGURED and DISCONNECTED stay distinct, because a UI that confuses
    them shows an admin a button that cannot work.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.domain.errors import WorkspaceAccessDeniedError
from app.domain.slack import SLACK_ADMIN_ROLES, SLACK_STATUSES
from app.domain.tenancy import WORKSPACE_ROLES, AuthorizedWorkspaceScope
from app.graphql.queries.memberships import WORKSPACE_NOT_FOUND_MESSAGE
from app.graphql.schema import build_schema
from app.graphql.types.slack import SlackIntegrationStatusType
from app.graphql.viewer import UNAUTHENTICATED_MESSAGE
from app.repositories.slack import SlackRepository
from app.services.slack import DatabaseTokenStore, SlackService

from tests.conftest import ExplodingPool, FakeConnection, FakePool, normalize


schema = build_schema("test")


INTEGRATION_QUERY = """
query SlackIntegration($slug: String!) {
  slackIntegration(workspaceSlug: $slug) {
    status
    teamName
    scopes
  }
}
"""

DISCONNECT_MUTATION = """
mutation SlackDisconnect($slug: String!) {
  slackDisconnect(input: { workspaceSlug: $slug }) {
    integration {
      status
      teamName
      scopes
    }
  }
}
"""

VIEWER_ID = UUID("00000000-0000-7000-8000-0000000000e1")
WORKSPACE_A = UUID("00000000-0000-7000-8000-00000000000a")
WORKSPACE_B = UUID("00000000-0000-7000-8000-00000000000b")

SLUG_A = "vector"
SLUG_B = "acme"

CONNECTED_AT = datetime(2026, 3, 1, 9, 0, 0, tzinfo=timezone.utc)


def scope(*, workspace_id=WORKSPACE_A, role="admin"):
    return AuthorizedWorkspaceScope(
        workspace_id=workspace_id,
        user_id=VIEWER_ID,
        role=role,
    )


def installation_row(*, workspace_id=WORKSPACE_A):
    """A row shaped like the repository's SELECT list, as a dict.

    Keyed by column name rather than by entity attribute, so a column renamed
    in the statement and not here fails the mapping the way a real row would.
    """
    return {
        "workspace_id": workspace_id,
        "slack_team_id": "T024BE7LD",
        "slack_team_name": "Vector HQ",
        "bot_user_id": "U0BOTBOT",
        "scopes": ["channels:read", "chat:write"],
        "connected_by_user_id": VIEWER_ID,
        "connected_at": CONNECTED_AT,
    }


class Context:
    """Stands in for VectorContext: a viewer, a membership service, a service.

    `viewer()` is awaited rather than read, mirroring the real context -- the
    identity comes from the request's session cookie, so a test expresses "not
    signed in" as `viewer_user_id=None`, which is what the session layer
    produces for a request with no cookie.
    """

    def __init__(self, *, membership_service, slack_service, viewer_user_id=VIEWER_ID):
        self.membership_service = membership_service
        self.slack_service = slack_service
        self._viewer_user_id = viewer_user_id

    async def viewer(self):
        if self._viewer_user_id is None:
            return None

        return SimpleNamespace(id=self._viewer_user_id)


class FakeMembershipService:
    """Resolves one slug, refuses every other, and records what it was asked."""

    def __init__(self, *, slug=SLUG_A, resolved=None):
        self._slug = slug
        self._scope = resolved if resolved is not None else scope()
        self.calls: list[dict] = []

    async def authorized_scope_for_slug(self, *, slug, user_id):
        self.calls.append({"slug": slug, "user_id": user_id})

        if slug != self._slug:
            raise WorkspaceAccessDeniedError()

        return self._scope


class ExplodingMembershipService:
    async def authorized_scope_for_slug(self, *, slug, user_id):
        raise AssertionError("an unauthenticated caller reached the service")


def slack_service(*, pool=None, configured=True):
    """The REAL service over a fake pool.

    Not a fake: the rules under test -- who may read a status, what an
    unconfigured deployment answers, what a disconnect leaves behind -- are
    this class's, and a fake would only assert that the test's copy of them
    agrees with itself.
    """
    return SlackService(
        pool=pool if pool is not None else FakePool(),
        repository=SlackRepository(),
        token_store=DatabaseTokenStore(),
        configured=configured,
    )


def context(*, membership=None, service=None, viewer_user_id=VIEWER_ID):
    return Context(
        membership_service=(
            membership if membership is not None else FakeMembershipService()
        ),
        slack_service=service if service is not None else slack_service(),
        viewer_user_id=viewer_user_id,
    )


async def run(document, *, slug=SLUG_A, **kwargs):
    return await schema.execute(
        document,
        variable_values={"slug": slug},
        context_value=context(**kwargs),
    )


def _deleted_table(query: str) -> str:
    """The table a `DELETE FROM x WHERE ...` names.

    Read out of the statement rather than matched against a whole normalised
    string, so the assertion below is about the ORDER of the deletes and does
    not also become a pin on their whitespace.
    """
    return normalize(query).split("DELETE FROM ", 1)[1].split()[0]


def only_error(result):
    assert result.errors is not None
    assert len(result.errors) == 1

    return result.errors[0].formatted


# --- the vocabularies, pinned -----------------------------------------


def test_the_status_enum_matches_the_domain_vocabulary():
    """Three copies of one list: the enum, SLACK_STATUSES, and the strings
    SlackService builds views with.

    Pinned rather than trusted, because a status the service produces and the
    enum does not carry raises inside `from_view` -- which reaches the client
    as a masked internal error, on the one field whose whole job is to be
    readable.
    """
    assert tuple(member.value for member in SlackIntegrationStatusType) == (
        SLACK_STATUSES
    )


def test_the_admin_roles_are_roles_the_schema_actually_has():
    """A typo here would refuse everyone, or admit nobody, silently.

    `SLACK_ADMIN_ROLES` is a subset of WORKSPACE_ROLES spelled out by hand
    rather than derived from a rank, so nothing but this test says the strings
    in it are ones a `workspace_members` row can hold.
    """
    assert SLACK_ADMIN_ROLES < set(WORKSPACE_ROLES)
    assert "member" not in SLACK_ADMIN_ROLES


# --- nothing secret is selectable -------------------------------------


@pytest.mark.parametrize(
    "field",
    ["botToken", "token", "signingSecret", "clientSecret", "botTokenReference"],
)
def test_no_credential_field_exists_on_the_integration_type(field):
    """The type has no field for a secret, which is why one cannot leak.

    Asserted against the built schema rather than by reading the class, so
    that a field added anywhere -- a resolver, a subclass, a merge -- fails
    here. A validation error is the pass condition: the schema does not know
    the field at all.
    """
    document = INTEGRATION_QUERY.replace("scopes", field)

    result = schema.execute_sync(document, variable_values={"slug": SLUG_A})

    assert result.errors is not None
    assert f"Cannot query field '{field}'" in result.errors[0].message


def test_the_sdl_mentions_no_slack_secret():
    """A whole-schema sweep, so a credential cannot arrive on another type.

    The previous test guards `SlackIntegration`; this one guards the schema,
    which is where a second Slack type would land.
    """
    sdl = schema.as_str().lower()

    for forbidden in ("bottoken", "signingsecret", "clientsecret", "accesstoken"):
        assert forbidden not in sdl


# --- an unidentified caller -------------------------------------------


@pytest.mark.parametrize(
    "document",
    [INTEGRATION_QUERY, DISCONNECT_MUTATION],
    ids=["slackIntegration", "slackDisconnect"],
)
async def test_an_unauthenticated_request_is_refused_before_any_lookup(document):
    """UNAUTHENTICATED, and no service is asked anything.

    The exploding membership service is the half that matters: a resolver
    that looked first and checked afterwards would produce this same response
    having already read a protected table for a stranger.
    """
    result = await run(
        document,
        membership=ExplodingMembershipService(),
        service=slack_service(pool=ExplodingPool()),
        viewer_user_id=None,
    )

    formatted = only_error(result)

    assert formatted["message"] == UNAUTHENTICATED_MESSAGE
    assert formatted["extensions"]["code"] == "UNAUTHENTICATED"


# --- tenancy: workspace A cannot reach workspace B ---------------------


@pytest.mark.parametrize(
    "document",
    [INTEGRATION_QUERY, DISCONNECT_MUTATION],
    ids=["slackIntegration", "slackDisconnect"],
)
async def test_a_member_of_one_workspace_cannot_reach_anothers_integration(document):
    """A NOT_FOUND that says nothing about whether workspace B exists.

    The membership lookup answers "no such workspace" and "not yours" with the
    same absent row, so the server never computes the difference; this asserts
    the transport does not reintroduce one. The exploding pool asserts the
    second half: no Slack table is read on behalf of a caller who was refused.
    """
    membership = FakeMembershipService(slug=SLUG_A)

    result = await schema.execute(
        document,
        variable_values={"slug": SLUG_B},
        context_value=context(
            membership=membership,
            service=slack_service(pool=ExplodingPool()),
        ),
    )

    formatted = only_error(result)

    assert formatted["message"] == WORKSPACE_NOT_FOUND_MESSAGE
    assert formatted["extensions"]["code"] == "NOT_FOUND"
    assert membership.calls == [{"slug": SLUG_B, "user_id": VIEWER_ID}]


@pytest.mark.parametrize(
    "document",
    [INTEGRATION_QUERY, DISCONNECT_MUTATION],
    ids=["slackIntegration", "slackDisconnect"],
)
async def test_a_plain_member_is_refused_exactly_as_a_stranger_is(document):
    """To a member who may not administer the workspace, this does not exist.

    Deliberately the same message and code a stranger gets. A distinct
    "forbidden" would tell every member of every workspace that this
    deployment has a Slack app and that admins can reach it.
    """
    result = await run(
        document,
        membership=FakeMembershipService(resolved=scope(role="member")),
        service=slack_service(pool=ExplodingPool()),
    )

    formatted = only_error(result)

    assert formatted["message"] == WORKSPACE_NOT_FOUND_MESSAGE
    assert formatted["extensions"]["code"] == "NOT_FOUND"


@pytest.mark.parametrize("role", sorted(SLACK_ADMIN_ROLES))
async def test_both_admin_roles_may_read_the_integration(role):
    """The control for the refusal above.

    Without it, a rule that refused everyone would pass every negative test
    in this file and ship an integration nobody can administer.
    """
    result = await run(
        INTEGRATION_QUERY,
        membership=FakeMembershipService(resolved=scope(role=role)),
    )

    assert result.errors is None
    assert result.data["slackIntegration"]["status"] == "DISCONNECTED"


# --- the three statuses -----------------------------------------------


async def test_an_unconfigured_deployment_answers_unconfigured_without_a_query():
    """No Slack app means no row can mean anything, so nothing is read.

    The exploding pool is the assertion: a status resolved from the database
    on a deployment with no credentials would be a query on every settings
    page load, answering a question the product cannot act on.
    """
    result = await run(
        INTEGRATION_QUERY,
        service=slack_service(pool=ExplodingPool(), configured=False),
    )

    assert result.errors is None
    assert result.data["slackIntegration"] == {
        "status": "UNCONFIGURED",
        "teamName": None,
        "scopes": [],
    }


async def test_a_configured_deployment_with_no_installation_answers_disconnected():
    """The distinction the whole feature is careful about.

    Same workspace, same caller, same absent row as the case above -- and a
    different answer, because the deployment can actually talk to Slack. This
    is the state that gets a connect button; UNCONFIGURED must not.
    """
    result = await run(INTEGRATION_QUERY, service=slack_service(pool=FakePool()))

    assert result.errors is None
    assert result.data["slackIntegration"] == {
        "status": "DISCONNECTED",
        "teamName": None,
        "scopes": [],
    }


async def test_a_connected_workspace_reports_its_team_and_granted_scopes():
    """The scopes come from the row, never from what this release requests.

    An admin can decline scopes individually, so a status built from
    REQUESTED_SCOPES would claim permissions the workspace never granted.
    """
    pool = FakePool(FakeConnection(row=installation_row()))

    result = await run(INTEGRATION_QUERY, service=slack_service(pool=pool))

    assert result.errors is None
    assert result.data["slackIntegration"] == {
        "status": "CONNECTED",
        "teamName": "Vector HQ",
        "scopes": ["channels:read", "chat:write"],
    }


# --- disconnect --------------------------------------------------------


async def test_disconnect_removes_the_installation_and_reports_the_new_state():
    connection = FakeConnection(row=installation_row())
    pool = FakePool(connection)

    result = await run(DISCONNECT_MUTATION, service=slack_service(pool=pool))

    assert result.errors is None
    assert result.data["slackDisconnect"]["integration"] == {
        "status": "DISCONNECTED",
        "teamName": None,
        "scopes": [],
    }

    deletes = [
        call for call in connection.queries if "DELETE FROM" in call["query"].upper()
    ]

    # Four tables, in dependency order, and the order is the assertion.
    #
    # Every foreign key in migration 018 is RESTRICT, so `slack_installations`
    # can only go last and `slack_notification_settings` -- the one table that
    # points at `slack_channels` -- can only go first. A disconnect that got
    # this wrong would fail against a real database on a workspace that had
    # configured anything, and would pass every behavioural test written
    # against a fake. Pinning the order is what makes that unshippable.
    assert [_deleted_table(call["query"]) for call in deletes] == [
        "slack_notification_settings",
        "slack_notification_preferences",
        "slack_channels",
        "slack_installations",
    ]

    # Every one of them scoped to the caller's own workspace, and by nothing
    # the client sent.
    assert [call["args"] for call in deletes] == [(WORKSPACE_A,)] * 4


async def test_disconnect_on_an_unconfigured_deployment_still_answers_unconfigured():
    """Not DISCONNECTED. The deployment has no Slack app either way.

    Reporting DISCONNECTED here would hand the client a state whose UI is a
    connect button -- offered immediately after an admin pressed disconnect,
    on a deployment where connecting cannot work.
    """
    result = await run(
        DISCONNECT_MUTATION,
        service=slack_service(pool=FakePool(), configured=False),
    )

    assert result.errors is None
    assert result.data["slackDisconnect"]["integration"]["status"] == "UNCONFIGURED"
