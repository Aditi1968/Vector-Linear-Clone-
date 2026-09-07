"""What the two OAuth flows put on the wire, and what the browser keeps.

Every other provider suite drives one leg at a time against a fake. These
check the two properties that only show up when the legs are compared with
each other, or when a real browser applies its own rules to a cookie -- which
is to say the two properties that produced live failures nothing here caught.

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
"""

import contextlib
import urllib.parse

from app.domain.slack import SlackOAuthError
from app.rest.github import STATE_COOKIE_NAME, STATE_COOKIE_PATH
from app.rest.slack import OAUTH_STATE_COOKIE_NAME, OAUTH_STATE_COOKIE_PATH
from app.services.slack import SlackOAuthClient, authorize_url


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
