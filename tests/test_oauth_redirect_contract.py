"""What the two OAuth flows put on the wire, and what the browser keeps.

Every other provider suite drives one leg at a time against a fake. These
check the properties that only show up when the legs are compared with each
other, or when a real browser applies its own rules to a cookie -- which is
to say the properties that produced live failures nothing here caught.

The first is that Slack's two legs must present the SAME redirect_uri. Slack
compares them and refuses the exchange when they differ, and each leg builds
its value in a different module, so nothing but a test that reads both can
say they agree.

The second is cookie semantics across a cross-site redirect. OAuth leaves for
the provider and comes back as a top-level GET from another site, so a state
cookie is only returned if it is `SameSite=Lax` (or None) and scoped to a
path covering the callback. Both were wrong in this repository at different
times: `SameSite` was right and the PATH was not, so the browser silently
withheld the cookie and the callback refused a flow that was entirely
correct. No unit test could see it, because a test client does not implement
cookie scoping.

The third is the same rule one level up, and it outlived the path fix: a
cookie goes back only to the HOST that set it. Both flows minted their state
on whatever origin the browser was on and both callbacks land on the origin
the provider has registered, so in development -- app on localhost, callback
on a tunnel -- the state was written somewhere the callback could never read
it, and both providers refused every install. The last section drives the
real routers across two origins through a jar that applies that rule.
"""

import contextlib
import dataclasses
import urllib.parse

import httpx
import pytest
from fastapi import FastAPI

from app.domain.slack import SlackOAuthError
from app.http_cookies import SESSION_COOKIE_NAME
from app.rest.github import STATE_COOKIE_NAME, STATE_COOKIE_PATH
from app.rest.github import build_services as github_dependency
from app.rest.github import router as github_router
from app.rest.slack import OAUTH_STATE_COOKIE_NAME, OAUTH_STATE_COOKIE_PATH
from app.rest.slack import router as slack_router
from app.rest.slack import slack_services as slack_dependency
from app.services.slack import SlackOAuthClient, authorize_url

from tests import test_github_rest, test_slack_rest


CALLBACK = "https://example.ngrok-free.app/integrations/slack/oauth/callback"


def redirect_uri_from(url: str) -> list[str]:
    """Every redirect_uri in a URL's query, decoded."""
    query = urllib.parse.urlparse(url).query

    return urllib.parse.parse_qs(query, keep_blank_values=True).get("redirect_uri", [])


# --------------------------------------------------------------------------
# Slack: the two legs must agree
# --------------------------------------------------------------------------


def test_the_authorize_leg_sends_the_configured_redirect_uri_byte_for_byte():
    sent = redirect_uri_from(
        authorize_url(client_id="123.456", state="s", redirect_uri=CALLBACK)
    )

    assert sent == [CALLBACK], "exactly one redirect_uri, unmodified"

    parsed = urllib.parse.urlparse(sent[0])

    # Asserted per component rather than as one string, because the failures
    # that actually happen are a trailing slash, a stray quote from a `.env`
    # line, or a query fragment appended by a well-meaning edit.
    assert parsed.scheme == "https"
    assert parsed.path == "/integrations/slack/oauth/callback"
    assert parsed.query == ""
    assert parsed.fragment == ""
    assert parsed.params == ""
    assert not parsed.path.endswith("/")


async def test_both_slack_legs_present_the_same_redirect_uri():
    """The property Slack actually enforces, checked across both modules."""
    posted: dict[str, str] = {}

    def capture(url: str, fields: dict[str, str]) -> dict[str, object]:
        posted.update(fields)

        # Deliberately not a well-formed grant. This test is about what goes
        # OUT, and building a response the parser accepts would couple it to
        # a shape it has no opinion about -- so the refusal is expected and
        # swallowed below.
        return {"ok": False, "error": "not_the_subject_of_this_test"}

    client = SlackOAuthClient(
        client_id="123.456", client_secret="shh", redirect_uri=CALLBACK
    )

    import app.services.slack as slack_module

    original = slack_module._post_form
    slack_module._post_form = capture

    try:
        with contextlib.suppress(SlackOAuthError):
            await client.exchange(code="the-code")
    finally:
        slack_module._post_form = original

    authorize_value = redirect_uri_from(
        authorize_url(client_id="123.456", state="s", redirect_uri=CALLBACK)
    )[0]

    assert posted.get("redirect_uri") == authorize_value


async def test_neither_leg_sends_a_redirect_uri_when_none_is_configured():
    """Absent on both, or present on both. Never one of each.

    Slack refuses the exchange when the legs disagree, and 'no redirect_uri'
    is a disagreement as much as a different one is.
    """
    posted: dict[str, str] = {}

    def capture(url: str, fields: dict[str, str]) -> dict[str, object]:
        posted.update(fields)

        return {"ok": False, "error": "not_the_subject_of_this_test"}

    import app.services.slack as slack_module

    original = slack_module._post_form
    slack_module._post_form = capture

    try:
        with contextlib.suppress(SlackOAuthError):
            await SlackOAuthClient(
                client_id="123.456", client_secret="shh", redirect_uri=None
            ).exchange(code="the-code")
    finally:
        slack_module._post_form = original

    assert "redirect_uri" not in posted
    assert redirect_uri_from(authorize_url(client_id="123.456", state="s")) == []


# --------------------------------------------------------------------------
# Both providers: the cookie has to come back across a cross-site redirect
# --------------------------------------------------------------------------


def test_every_oauth_state_cookie_is_scoped_to_reach_its_callback():
    """Root path, for both providers, and the reason is the browser.

    The flows begin under one prefix and the providers redirect to a callback
    registered under another, so a cookie scoped to either prefix is never
    sent to the other. That is not a policy this code can argue with: it is
    RFC 6265 path matching, applied by the browser, invisible to every test
    that does not implement it.

    Asserted as an exact equality rather than a `startswith`, so narrowing
    the scope back to `/github` or `/slack/oauth` -- which reads like
    tightening security and is actually an outage -- fails here.
    """
    assert STATE_COOKIE_PATH == "/"
    assert OAUTH_STATE_COOKIE_PATH == "/"


def test_the_two_providers_do_not_share_a_state_cookie_name():
    """One flow must not consume or overwrite the other's state.

    Both are now scoped to the whole origin, so the names are the only thing
    keeping them apart -- and a collision would make starting one flow
    silently invalidate the other.
    """
    assert STATE_COOKIE_NAME != OAUTH_STATE_COOKIE_NAME


# --------------------------------------------------------------------------
# Both providers: the state has to be minted on the callback's HOST
# --------------------------------------------------------------------------
#
# The path was fixed once (above) and the host was still wrong. A cookie is
# returned only to the host that set it, and the callback's host belongs to
# the provider's registration, not to wherever the browser happened to be --
# so an install started from the dev origin came back to a callback holding
# no state and was refused. Correctly: the check was right and the start leg
# was in the wrong place. Both providers failed for the one reason.
#
# These drive the real routers through httpx's cookie jar, which applies the
# same host rule a browser does, across two origins. Nothing here fakes the
# refusal: the same jar has to be told no when the state is absent or was
# issued to somebody else.

APP_HOST = "app.test"
CALLBACK_HOST = "callback.test"

# Dotted on purpose. `http.cookiejar` rewrites a host with no dot in it to
# `<host>.local`, so a test written against a bare `localhost` would be
# exercising the jar's quirk rather than the rule.
APP_ORIGIN = f"http://{APP_HOST}"
CALLBACK_ORIGIN = f"https://{CALLBACK_HOST}"

SESSION_TOKEN = test_github_rest.SESSION_TOKEN


@dataclasses.dataclass(frozen=True)
class Flow:
    """One provider's OAuth round trip, reduced to what differs between them."""

    application: FastAPI
    start: str
    callback: str
    state_cookie: str


def github_flow() -> Flow:
    """The GitHub install, with its callback registered on another host."""
    services = dataclasses.replace(
        test_github_rest.build_services_for(),
        oauth_callback_url=f"{CALLBACK_ORIGIN}/integrations/github/oauth/callback",
    )

    application = FastAPI()
    application.include_router(github_router)
    application.include_router(github_router, prefix="/integrations")
    application.dependency_overrides[github_dependency] = lambda: services

    return Flow(
        application=application,
        start=f"/github/install?workspace={test_github_rest.WORKSPACE_SLUG}",
        callback=(
            "/integrations/github/oauth/callback"
            f"?state={{state}}&installation_id={test_github_rest.INSTALLATION_ID}"
        ),
        state_cookie=STATE_COOKIE_NAME,
    )


def slack_flow() -> Flow:
    """The Slack installation, likewise."""
    services = dataclasses.replace(
        test_slack_rest.build_services(),
        oauth_callback_url=f"{CALLBACK_ORIGIN}/integrations/slack/oauth/callback",
    )

    application = FastAPI()
    application.include_router(slack_router)
    application.include_router(slack_router, prefix="/integrations")
    application.dependency_overrides[slack_dependency] = lambda: services

    return Flow(
        application=application,
        start=f"/slack/oauth/start?workspace={test_slack_rest.WORKSPACE_SLUG}",
        callback="/integrations/slack/oauth/callback?state={state}&code=the-code",
        state_cookie=OAUTH_STATE_COOKIE_NAME,
    )


FLOWS = [github_flow, slack_flow]


def browser(flow: Flow) -> httpx.AsyncClient:
    """A client with one cookie jar, signed in on both origins.

    The session is placed on each host by hand because a browser would have
    one there -- the failing deployment did, which is exactly what proved the
    cookie ATTRIBUTES were fine: `vector_session` carries the same Lax,
    non-Secure, path-`/` policy and arrived on the cross-site callback
    perfectly well. Only the state, minted on the other host, did not.
    """
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=flow.application),
        base_url=APP_ORIGIN,
    )

    for host in (APP_HOST, CALLBACK_HOST):
        client.cookies.set(SESSION_COOKIE_NAME, SESSION_TOKEN, domain=host)

    return client


async def hand_off_to_provider(client: httpx.AsyncClient, flow: Flow) -> httpx.Response:
    """Walk the start leg from the app's origin until it leaves for the provider.

    Redirects are followed by hand rather than by httpx, because the last one
    goes to github.com or slack.com and the ASGI transport would answer it
    with this application.
    """
    url = f"{APP_ORIGIN}{flow.start}"

    for _ in range(3):
        response = await client.get(url)
        location = response.headers.get("location", "")

        if not location.startswith((APP_ORIGIN, CALLBACK_ORIGIN)):
            return response

        url = location

    raise AssertionError("the start leg redirected in a loop")


def state_of(handoff: httpx.Response) -> str:
    """The state the provider was asked to echo back."""
    query = urllib.parse.urlparse(handoff.headers["location"]).query

    return urllib.parse.parse_qs(query)["state"][0]


@pytest.mark.parametrize("build", FLOWS, ids=["github", "slack"])
async def test_a_flow_started_elsewhere_moves_to_the_callback_origin_first(build):
    """No state is minted on a host the callback will never be sent from.

    The hop carries this route's own path and query to the configured
    callback host and nothing else, so there is no parameter for a caller to
    aim it with.
    """
    flow = build()

    async with browser(flow) as client:
        response = await client.get(f"{APP_ORIGIN}{flow.start}")

    assert response.status_code == 302
    assert response.headers["location"] == f"{CALLBACK_ORIGIN}{flow.start}"
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize("build", FLOWS, ids=["github", "slack"])
async def test_the_state_a_browser_receives_is_the_one_the_callback_accepts(build):
    """The whole round trip, through a jar that applies the host rule.

    This is the half that was broken in production: every check below was
    already correct, and the cookie they checked had been written to a host
    the callback never sees.
    """
    flow = build()

    async with browser(flow) as client:
        handoff = await hand_off_to_provider(client, flow)
        state = state_of(handoff)

        response = await client.get(CALLBACK_ORIGIN + flow.callback.format(state=state))

    assert response.status_code < 400, response.text


@pytest.mark.parametrize("build", FLOWS, ids=["github", "slack"])
async def test_the_callback_still_refuses_a_state_this_browser_never_received(build):
    """The other half, and the one a passing happy path would hide.

    A test that only proved acceptance would pass just as well against a
    build with the state check deleted. Both refusals run through the same
    jar and the same routes as the acceptance above.
    """
    flow = build()

    async with browser(flow) as first, browser(flow) as second:
        stolen = state_of(await hand_off_to_provider(first, flow))
        finish = CALLBACK_ORIGIN + flow.callback.format(state=stolen)

        # A browser that started nothing, holding no state cookie at all.
        assert (await second.get(finish)).status_code == 400

        # And one part-way through a flow of its own, presenting a state that
        # belongs to the first browser.
        await hand_off_to_provider(second, flow)

        assert (await second.get(finish)).status_code == 400
