"""The Slack REST surface: OAuth state, and the event feed. No network.

Every test here runs against the real router with the real signature check
and the real state handling. What is faked is everything below the transport
-- the services, the OAuth exchange -- because there are no Slack credentials
in this environment and there must not need to be: an integration whose only
exercise is a live app is one whose failure modes are discovered by users.

Two properties get most of the attention, because they are the two an
attacker reaches without a session:

  * a state that this server did not issue to THIS browser is refused, and a
    state that has been used once is refused ever after;
  * an event that Vector itself caused is not ingested, and an event Slack
    redelivers is claimed exactly once.
"""

import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI

from app.domain.errors import WorkspaceAccessDeniedError
from app.domain.slack import (
    SlackGrant,
    SlackInstallationEntity,
    SlackIntegrationView,
    SlackOAuthError,
    SlackTeamAlreadyConnectedError,
)
from app.domain.tenancy import AuthorizedWorkspaceScope
from app.rest.slack import (
    DEFAULT_RETURN_TEMPLATE,
    MAX_TIMESTAMP_AGE_SECONDS,
    OAUTH_STATE_COOKIE_NAME,
    RETURN_PATH_TEMPLATES,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    SlackRequestServices,
    _parse_state_cookie,
    slack_services,
)
from app.rest.slack import (
    router as slack_router,
)
from app.services.slack import SLACK_AUTHORIZE_URL, SlackService


SIGNING_SECRET = "8f742231b10e8888abcd99yyyzzz85a5"
CLIENT_ID = "123456789.987654321"

BASE_URL = "http://vector.test"

WORKSPACE_SLUG = "vector"
OTHER_SLUG = "acme"

VIEWER_ID = UUID("00000000-0000-7000-8000-0000000000e1")
WORKSPACE_ID = UUID("00000000-0000-7000-8000-000000000001")

SLACK_TEAM_ID = "T024BE7LD"
BOT_USER_ID = "U0BOTBOT"
HUMAN_USER_ID = "U0HUMAN1"

CONNECTED_AT = datetime(2026, 3, 1, 9, 0, 0, tzinfo=timezone.utc)


def admin_scope(role="admin"):
    return AuthorizedWorkspaceScope(
        workspace_id=WORKSPACE_ID,
        user_id=VIEWER_ID,
        role=role,
    )


def installation():
    return SlackInstallationEntity(
        workspace_id=WORKSPACE_ID,
        slack_team_id=SLACK_TEAM_ID,
        slack_team_name="Vector HQ",
        bot_user_id=BOT_USER_ID,
        scopes=("chat:write",),
        connected_by_user_id=VIEWER_ID,
        connected_at=CONNECTED_AT,
    )


def grant():
    return SlackGrant(
        slack_team_id=SLACK_TEAM_ID,
        slack_team_name="Vector HQ",
        bot_user_id=BOT_USER_ID,
        scopes=("chat:write",),
        bot_token="xoxb-not-a-real-token",
    )


# --- fakes for everything below the transport --------------------------


class FakeAuth:
    """Authenticates whoever the test says, from whatever token arrives."""

    def __init__(self, viewer_id=VIEWER_ID):
        self._viewer_id = viewer_id

    async def authenticate(self, token):
        if self._viewer_id is None:
            return None

        return SimpleNamespace(id=self._viewer_id)


class FakeMembership:
    """Answers for one slug and refuses every other, like the real lookup.

    The refusal is `WorkspaceAccessDeniedError` for both "no such workspace"
    and "not yours", because that is the only answer the real repository can
    produce -- it resolves the pair in one statement.
    """

    def __init__(self, *, slug=WORKSPACE_SLUG, scope=None):
        self._slug = slug
        self._scope = scope if scope is not None else admin_scope()
        self.calls: list[dict] = []

    async def authorized_scope_for_slug(self, *, slug, user_id):
        self.calls.append({"slug": slug, "user_id": user_id})

        if slug != self._slug:
            raise WorkspaceAccessDeniedError()

        return self._scope


class FakeSlackService:
    """Records what the transport asked of it; enforces the real admin rule.

    `require_admin` delegates to the real staticmethod rather than
    reimplementing it, so a test that passes here is not passing against a
    laxer copy of the rule than production runs.
    """

    def __init__(self, *, install=None, claims=None, connect_error=None):
        self._install = install
        self._claims = list(claims) if claims is not None else []
        self._connect_error = connect_error
        self.claimed: list[str] = []
        self.lookups: list[str] = []
        self.connects: list[SlackGrant] = []

    @staticmethod
    def require_admin(scope):
        SlackService.require_admin(scope)

    async def installation_for_team(self, *, slack_team_id):
        self.lookups.append(slack_team_id)

        return self._install

    async def claim_event(self, *, event_id):
        self.claimed.append(event_id)

        # Defaults to "first delivery" so a test only has to say when it is
        # modelling a redelivery.
        return self._claims.pop(0) if self._claims else True

    async def connect(self, *, scope, grant):
        self.connects.append(grant)

        if self._connect_error is not None:
            raise self._connect_error

        return SlackIntegrationView(
            status="connected",
            team_name=grant.slack_team_name,
            scopes=grant.scopes,
        )


class FakeExchange:
    """Stands in for the one outbound HTTP call in the feature."""

    def __init__(self, *, result=None, error=None):
        self._result = result if result is not None else grant()
        self._error = error
        self.codes: list[str] = []

    async def exchange(self, *, code):
        self.codes.append(code)

        if self._error is not None:
            raise self._error

        return self._result


class ExplodingSlackService(FakeSlackService):
    """Fails if the transport reaches it. Refused requests must not."""

    async def installation_for_team(self, *, slack_team_id):
        raise AssertionError("a refused request reached the Slack service")

    async def claim_event(self, *, event_id):
        raise AssertionError("a refused request reached the Slack service")

    async def connect(self, *, scope, grant):
        raise AssertionError("a refused request reached the Slack service")


def build_services(
    *,
    configured=True,
    auth=None,
    membership=None,
    slack=None,
    oauth=None,
):
    """A SlackRequestServices with no pool, no settings and no credentials.

    `configured=False` clears all three configuration-dependent fields
    together, which is what an UNCONFIGURED deployment actually looks like --
    see Settings.slack_configured for why it is all or nothing.
    """
    return SlackRequestServices(
        environment="test",
        client_id=CLIENT_ID if configured else None,
        signing_secret=SIGNING_SECRET if configured else None,
        auth=auth if auth is not None else FakeAuth(),
        membership=membership if membership is not None else FakeMembership(),
        slack=slack if slack is not None else FakeSlackService(),
        oauth=(oauth if oauth is not None else FakeExchange()) if configured else None,
    )


def build_client(services) -> httpx.AsyncClient:
    """The router in a bare application, spoken to in process.

    A bare FastAPI rather than `create_app()`, so these tests need no
    DATABASE_URL and no lifespan. The composed application is covered by
    tests/test_slack_composition.py, which is where "is it mounted at all"
    belongs.
    """
    application = FastAPI()
    application.include_router(slack_router)
    application.dependency_overrides[slack_services] = lambda: services

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url=BASE_URL,
    )


def state_cookie(client) -> str | None:
    """The raw state cookie this client holds, without transport quoting.

    `Set-Cookie` quotes any value containing a character outside the cookie
    grammar, and the return path holds a "/". Starlette unquotes on the way
    back in -- which is why the server round-trips correctly -- so the
    stripping belongs here, in the one place a test reads the jar directly.
    """
    raw = client.cookies.get(OAUTH_STATE_COOKIE_NAME)

    return None if raw is None else raw.strip('"')


def sign(body: bytes, *, secret=SIGNING_SECRET, timestamp=None):
    """(headers) that make `body` look like a genuine Slack delivery."""
    stamp = timestamp if timestamp is not None else str(int(time.time()))
    base = f"v0:{stamp}:{body.decode('utf-8')}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), base, hashlib.sha256).hexdigest()

    return {
        TIMESTAMP_HEADER: stamp,
        SIGNATURE_HEADER: f"v0={digest}",
        "content-type": "application/json",
    }


def event_body(**overrides):
    """A `message` event_callback, valid unless a test breaks it."""
    payload = {
        "type": "event_callback",
        "event_id": "Ev0001",
        "team_id": SLACK_TEAM_ID,
        "event": {
            "type": "message",
            "user": HUMAN_USER_ID,
            "text": "ship it",
        },
    }

    return json.dumps(payload | overrides).encode("utf-8")


async def post_event(client, body, *, headers=None):
    return await client.post(
        "/slack/events",
        content=body,
        headers=headers if headers is not None else sign(body),
    )


# --- an unconfigured deployment ----------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", f"/slack/oauth/start?workspace={WORKSPACE_SLUG}"),
        ("GET", "/slack/oauth/callback?code=c&state=s"),
        ("POST", "/slack/events"),
    ],
    ids=["start", "callback", "events"],
)
async def test_every_route_answers_503_with_no_slack_configuration(method, path):
    """Mounted, reachable, and honest about having nothing to work with.

    503 rather than 404, because the routes exist: a 404 would make a
    deployment that forgot the credentials indistinguishable from one running
    a build without the feature, and would mean the routing table differs
    between environments.

    The refusal comes before anything else -- the exploding service asserts
    that no lookup, claim or connect happens on the way to it.
    """
    services = build_services(configured=False, slack=ExplodingSlackService())

    async with build_client(services) as client:
        response = await client.request(method, path)

    assert response.status_code == 503
    assert response.json()["detail"] == "Slack is not configured"


# --- OAuth start: authorization before a state exists ------------------


async def test_start_sends_an_admin_to_slack_with_a_state_cookie():
    async with build_client(build_services()) as client:
        response = await client.get(f"/slack/oauth/start?workspace={WORKSPACE_SLUG}")

    assert response.status_code == 303

    location = response.headers["location"]

    assert location.startswith(SLACK_AUTHORIZE_URL)
    assert f"client_id={CLIENT_ID.replace('.', '%2E')}" in location or (
        f"client_id={CLIENT_ID}" in location
    )

    # The state in the URL is the state in the cookie, which is the whole
    # binding: the callback compares the two.
    cookie = state_cookie(client)

    assert cookie is not None

    state = cookie.split(":")[0]

    assert f"state={state}" in location


async def test_start_refuses_an_unauthenticated_caller_before_issuing_a_state():
    """No session, no state. There is nothing to carry through Slack."""
    services = build_services(auth=FakeAuth(viewer_id=None))

    async with build_client(services) as client:
        response = await client.get(f"/slack/oauth/start?workspace={WORKSPACE_SLUG}")

    assert response.status_code == 401
    assert state_cookie(client) is None


@pytest.mark.parametrize(
    ("slug", "role"),
    [
        (OTHER_SLUG, "admin"),
        (WORKSPACE_SLUG, "member"),
    ],
    ids=["not a member of that workspace", "a member but not an admin"],
)
async def test_start_answers_the_same_404_for_a_stranger_and_a_plain_member(slug, role):
    """One answer, so a member cannot learn that Slack is configured here."""
    services = build_services(
        membership=FakeMembership(scope=admin_scope(role=role)),
    )

    async with build_client(services) as client:
        response = await client.get(f"/slack/oauth/start?workspace={slug}")

    assert response.status_code == 404
    assert response.json()["detail"] == "Workspace not found"
    assert state_cookie(client) is None


@pytest.mark.parametrize(
    "requested",
    ["https://evil.example/steal", "//evil.example", "/etc/passwd", "javascript:1"],
)
async def test_a_return_path_outside_the_allowlist_never_reaches_the_cookie(
    requested,
):
    """The redirect target is chosen from a set, never parsed from a request.

    Asserted at the point the value is stored rather than only at the
    redirect, because a cookie holding an attacker's URL is already a bug --
    it only needs one later reader that trusts it.
    """
    async with build_client(build_services()) as client:
        await client.get(
            "/slack/oauth/start",
            params={"workspace": WORKSPACE_SLUG, "return_to": requested},
        )

        cookie = state_cookie(client)

    assert cookie is not None

    # Read back through the module's own parser rather than by splitting on
    # colons here: the cookie gained a session-digest field, and a test that
    # unpacks a fixed number of parts fails on the shape rather than on the
    # thing it is about.
    pending = _parse_state_cookie(cookie)

    assert pending is not None
    assert pending.return_path == DEFAULT_RETURN_TEMPLATE
    assert pending.return_path in RETURN_PATH_TEMPLATES


# --- OAuth callback: state validation ----------------------------------


async def start_flow(client):
    """Run the start route and return the state it issued to this client."""
    await client.get(f"/slack/oauth/start?workspace={WORKSPACE_SLUG}")

    return client.cookies[OAUTH_STATE_COOKIE_NAME].strip('"').split(":")[0]


async def test_a_matching_state_completes_the_flow():
    exchange = FakeExchange()
    slack = FakeSlackService()
    services = build_services(oauth=exchange, slack=slack)

    async with build_client(services) as client:
        state = await start_flow(client)

        response = await client.get(
            "/slack/oauth/callback",
            params={"code": "auth-code", "state": state},
        )

    assert response.status_code == 303
    assert response.headers["location"] == DEFAULT_RETURN_TEMPLATE.format(
        slug=WORKSPACE_SLUG
    )
    assert exchange.codes == ["auth-code"]
    assert slack.connects and slack.connects[0].slack_team_id == SLACK_TEAM_ID


async def test_a_callback_with_no_state_cookie_is_refused():
    """A callback arriving in a browser that started no flow is CSRF.

    This is the shape of the attack the state exists for: an attacker sends
    the admin's browser to the callback with a code from a Slack workspace
    they control. Without a matching cookie there is nothing to match, and
    the exploding service asserts no connect was attempted.
    """
    services = build_services(slack=ExplodingSlackService())

    async with build_client(services) as client:
        response = await client.get(
            "/slack/oauth/callback",
            params={"code": "attacker-code", "state": "anything"},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid OAuth state"


async def test_a_foreign_state_is_refused():
    """A state minted for one browser does not work in another.

    Two clients, two flows, and the second presents the first's state. The
    cookie is what binds a state to a browser, so the mismatch is refused even
    though the state itself was genuinely issued by this server.
    """
    services = build_services(slack=ExplodingSlackService())

    async with build_client(build_services()) as victim:
        stolen = await start_flow(victim)

    async with build_client(services) as attacker:
        # The attacker starts their own flow, so they hold a valid cookie --
        # just not the one that matches the state they present.
        await start_flow(attacker)

        response = await attacker.get(
            "/slack/oauth/callback",
            params={"code": "attacker-code", "state": stolen},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid OAuth state"


async def test_a_state_is_single_use_and_a_replay_is_refused():
    """The second identical callback finds no cookie and is refused.

    Single use is enforced by clearing the cookie the moment the state
    matched, so this test is also the one that would fail if the delete ever
    stopped addressing the same cookie the set wrote (a changed path, a
    changed flag).
    """
    slack = FakeSlackService()
    services = build_services(slack=slack)

    async with build_client(services) as client:
        state = await start_flow(client)

        first = await client.get(
            "/slack/oauth/callback",
            params={"code": "auth-code", "state": state},
        )

        assert first.status_code == 303
        assert state_cookie(client) is None

        replay = await client.get(
            "/slack/oauth/callback",
            params={"code": "auth-code", "state": state},
        )

    assert replay.status_code == 400
    assert replay.json()["detail"] == "Invalid OAuth state"

    # One connect, not two: the replay never reached the service.
    assert len(slack.connects) == 1


async def test_a_mismatched_state_does_not_burn_the_pending_flow():
    """A forged callback must not cancel a legitimate flow in progress.

    The cookie is consumed only when the state matches. Clearing it on a
    mismatch would let anyone who can send the admin's browser one request
    break every installation attempt.
    """
    async with build_client(build_services()) as client:
        state = await start_flow(client)

        await client.get(
            "/slack/oauth/callback",
            params={"code": "c", "state": "not-the-state"},
        )

        assert state_cookie(client) is not None

        response = await client.get(
            "/slack/oauth/callback",
            params={"code": "auth-code", "state": state},
        )

    assert response.status_code == 303


async def test_a_declined_installation_returns_the_admin_to_the_product():
    """`error=access_denied` is a decision, not a failure.

    The value is never echoed: it is a string from a query parameter, and the
    redirect goes to the allowlisted path the flow started with.
    """
    slack = FakeSlackService()

    async with build_client(build_services(slack=slack)) as client:
        state = await start_flow(client)

        response = await client.get(
            "/slack/oauth/callback",
            params={"error": "access_denied", "state": state},
        )

    assert response.status_code == 303
    assert response.headers["location"] == DEFAULT_RETURN_TEMPLATE.format(
        slug=WORKSPACE_SLUG
    )
    assert slack.connects == []


async def test_a_refused_exchange_consumes_the_state_and_reports_400():
    services = build_services(oauth=FakeExchange(error=SlackOAuthError()))

    async with build_client(services) as client:
        state = await start_flow(client)

        response = await client.get(
            "/slack/oauth/callback",
            params={"code": "stale-code", "state": state},
        )

        assert state_cookie(client) is None

    assert response.status_code == 400


async def test_a_slack_workspace_already_connected_elsewhere_reports_409():
    services = build_services(
        slack=FakeSlackService(connect_error=SlackTeamAlreadyConnectedError()),
    )

    async with build_client(services) as client:
        state = await start_flow(client)

        response = await client.get(
            "/slack/oauth/callback",
            params={"code": "auth-code", "state": state},
        )

    assert response.status_code == 409
    # It names neither Vector workspace; see SlackTeamAlreadyConnectedError.
    assert response.json()["detail"] == "That Slack workspace is already connected"


# --- the events endpoint -----------------------------------------------


async def test_a_correctly_signed_event_is_accepted_and_claimed_once():
    slack = FakeSlackService(install=installation())

    async with build_client(build_services(slack=slack)) as client:
        response = await post_event(client, event_body())

    assert response.status_code == 200
    assert slack.claimed == ["Ev0001"]


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {TIMESTAMP_HEADER: "1700000000"},
        {SIGNATURE_HEADER: "v0=" + "0" * 64},
    ],
    ids=["no headers", "timestamp only", "signature only"],
)
async def test_an_unsigned_event_is_refused_and_never_reaches_a_service(headers):
    services = build_services(slack=ExplodingSlackService())

    async with build_client(services) as client:
        response = await post_event(client, event_body(), headers=headers)

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid request signature"


async def test_a_tampered_body_is_refused_by_the_route():
    """Signed one body, sent another. The route verifies the bytes it got."""
    services = build_services(slack=ExplodingSlackService())
    body = event_body()

    async with build_client(services) as client:
        response = await post_event(
            client,
            body.replace(b"Ev0001", b"Ev9999"),
            headers=sign(body),
        )

    assert response.status_code == 401


async def test_a_stale_delivery_is_refused_by_the_route():
    services = build_services(slack=ExplodingSlackService())
    body = event_body()
    stale = str(int(time.time()) - MAX_TIMESTAMP_AGE_SECONDS - 60)

    async with build_client(services) as client:
        response = await post_event(client, body, headers=sign(body, timestamp=stale))

    assert response.status_code == 401


async def test_the_url_verification_challenge_is_answered():
    """Signed like everything else, and echoed back verbatim.

    Answered after verification rather than before it: an unsigned handshake
    is somebody else asking whether this endpoint exists.
    """
    body = json.dumps({"type": "url_verification", "challenge": "3eZbrw1aB"}).encode(
        "utf-8"
    )

    async with build_client(build_services()) as client:
        response = await post_event(client, body)

    assert response.status_code == 200
    assert response.json() == {"challenge": "3eZbrw1aB"}


async def test_an_unsigned_challenge_is_refused():
    async with build_client(build_services()) as client:
        response = await post_event(
            client,
            json.dumps({"type": "url_verification", "challenge": "x"}).encode(),
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 401


@pytest.mark.parametrize(
    "event",
    [
        {"type": "message", "bot_id": "B0ANYAPP", "text": "hello"},
        {"type": "message", "subtype": "bot_message", "text": "hello"},
        {"type": "message", "user": BOT_USER_ID, "text": "hello"},
    ],
    ids=["any app's bot_id", "legacy bot_message subtype", "our own bot user"],
)
async def test_an_event_vector_caused_is_ignored_rather_than_ingested(event):
    """Loop prevention, on all three signals Slack marks bot traffic with.

    Ignored means not claimed: the event never reaches the deduplication
    ledger, because it is not going to be acted on. If any of these were
    ingested and Vector answered by posting, Slack would deliver the answer
    back as another event and the loop would not stop on its own.
    """
    slack = FakeSlackService(install=installation())

    async with build_client(build_services(slack=slack)) as client:
        response = await post_event(client, event_body(event=event))

    assert response.status_code == 200
    assert slack.claimed == []


async def test_a_human_event_is_not_mistaken_for_a_bot_event():
    """The control for the case above: the same shape, a different author.

    Without it, a loop check that ignored everything would pass every test in
    the group above and quietly disable the integration.
    """
    slack = FakeSlackService(install=installation())

    async with build_client(build_services(slack=slack)) as client:
        await post_event(client, event_body())

    assert slack.claimed == ["Ev0001"]


async def test_a_redelivered_event_is_processed_once():
    """Slack retries; the ledger is what makes the second delivery a no-op.

    Both deliveries are acknowledged with 200 -- a non-2xx would ask Slack to
    retry again -- and the claim reports "first" only once. The claim is what
    a future ingestion step will hang off, so "processed once" is exactly
    "claimed once" here.
    """
    slack = FakeSlackService(install=installation(), claims=[True, False])

    async with build_client(build_services(slack=slack)) as client:
        first = await post_event(client, event_body())
        second = await post_event(client, event_body())

    assert (first.status_code, second.status_code) == (200, 200)
    assert slack.claimed == ["Ev0001", "Ev0001"]


async def test_an_event_for_an_unknown_slack_workspace_is_acknowledged():
    """Nothing to route it to, so nothing is claimed and nothing is an error.

    Slack delivers events for every workspace an app is installed in,
    including ones this deployment has disconnected. Answering non-2xx would
    have Slack retry an event that can never be handled.
    """
    slack = FakeSlackService(install=None)

    async with build_client(build_services(slack=slack)) as client:
        response = await post_event(client, event_body())

    assert response.status_code == 200
    assert slack.lookups == [SLACK_TEAM_ID]
    assert slack.claimed == []


@pytest.mark.parametrize(
    "body",
    [b"not json", b"[]", b'"a string"'],
    ids=["unparseable", "a list", "a bare string"],
)
async def test_a_signed_but_malformed_payload_is_reported_as_400(body):
    """Signed by Slack and still nonsense. A 400, not a 500 and not a 200.

    Parsed only after verification, so this path is reachable only by someone
    holding the signing secret -- which is why it may say "malformed" rather
    than having to be indistinguishable from a bad signature.
    """
    services = build_services(slack=ExplodingSlackService())

    async with build_client(services) as client:
        response = await post_event(client, body)

    assert response.status_code == 400
