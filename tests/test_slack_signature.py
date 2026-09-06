"""Slack's v0 request signature, as a pure function. No app, no network.

This is the only thing standing between Slack's event endpoint and anyone on
the internet who knows its URL, so it is tested as a function rather than only
through the route: every way a request can fail to be genuine gets its own
case, and each one asserts the refusal rather than merely "not a 200".

The helper that signs a request here builds its base string by formatting and
encoding a str, while the implementation concatenates bytes. That divergence
is deliberate -- two spellings of the same construction disagree if either one
gets the order, the separators or the prefix wrong -- and it is backed up by
KNOWN_GOOD_SIGNATURE below, which pins the wire format against a literal so a
refactor cannot quietly redefine what "signed" means on both sides at once.
"""

import hashlib
import hmac

import pytest

from app.rest.slack import (
    MAX_TIMESTAMP_AGE_SECONDS,
    verify_slack_signature,
)


SIGNING_SECRET = "8f742231b10e8888abcd99yyyzzz85a5"
OTHER_SECRET = "0000000000000000000000000000abcd"

BODY = b'{"type":"event_callback","event_id":"Ev0001"}'

# A fixed instant, so "fresh" and "stale" are properties of the case rather
# than of when the suite runs.
NOW = 1700000000.0
TIMESTAMP = "1700000000"

# The signature the scheme produces for (SIGNING_SECRET, TIMESTAMP, BODY),
# pinned as a literal.
#
# Every other case here signs with a helper, so all of them would keep passing
# if the base string were redefined -- the helper and the implementation would
# simply be wrong together. This one cannot: the value was computed once from
# `v0:{timestamp}:{body}` and only that construction reproduces it. It is the
# test that fails if someone "simplifies" the scheme.
KNOWN_GOOD_SIGNATURE = (
    "v0=db917b7f9c95d2c240833cf6621658877cee1943900d163400baf0190cdb5a3e"
)


def sign(body: bytes, *, secret: str = SIGNING_SECRET, timestamp: str = TIMESTAMP):
    """Sign a request the way Slack does, spelled differently on purpose."""
    base = f"v0:{timestamp}:{body.decode('utf-8')}".encode("utf-8")

    return "v0=" + hmac.new(secret.encode("utf-8"), base, hashlib.sha256).hexdigest()


def verify(**overrides):
    """verify_slack_signature over a valid request, with fields replaced."""
    arguments = {
        "signing_secret": SIGNING_SECRET,
        "timestamp": TIMESTAMP,
        "signature": sign(BODY),
        "body": BODY,
        "now": NOW,
    }

    return verify_slack_signature(**(arguments | overrides))


# --- the signature the scheme actually produces ------------------------


def test_the_helper_and_the_implementation_agree_on_a_pinned_value():
    """Both spellings of the base string produce the documented literal.

    Guards the whole file: if this fails, every other case is asserting
    against a construction that is no longer Slack's, and the negative tests
    below would go on passing while the endpoint accepted nothing real.
    """
    assert sign(BODY) == KNOWN_GOOD_SIGNATURE
    assert verify(signature=KNOWN_GOOD_SIGNATURE) is True


def test_a_correctly_signed_recent_request_is_accepted():
    assert verify() is True


# --- the four ways a request is refused --------------------------------


def test_a_tampered_body_is_refused():
    """The signature covers the bytes, so changing one invalidates it.

    The case the raw-body rule exists for: a proxy, a parser or a
    re-serialisation between the socket and this check would produce exactly
    this failure -- or, worse, would not, having verified a body Slack never
    sent.
    """
    assert verify(body=BODY.replace(b"Ev0001", b"Ev0002")) is False


def test_a_signature_made_with_the_wrong_secret_is_refused():
    """The only thing that makes a signature ours is the key it was made with."""
    assert verify(signature=sign(BODY, secret=OTHER_SECRET)) is False


@pytest.mark.parametrize(
    ("timestamp", "signature"),
    [
        (None, sign(BODY)),
        (TIMESTAMP, None),
        (None, None),
        ("", sign(BODY)),
        (TIMESTAMP, ""),
    ],
    ids=[
        "no timestamp header",
        "no signature header",
        "neither header",
        "empty timestamp header",
        "empty signature header",
    ],
)
def test_a_missing_header_is_refused(timestamp, signature):
    """An absent header is not a pass, and an empty one is not absent-but-fine.

    Both spellings matter because Starlette answers a missing header with
    None and a present-but-empty one with "", and a check written as
    `if timestamp is None` would let the second through to `int("")`.
    """
    assert verify(timestamp=timestamp, signature=signature) is False


def test_a_stale_timestamp_is_refused_even_though_the_signature_is_valid():
    """A replay: correctly signed, and hours old.

    The signature is genuine here -- it is computed over the stale timestamp
    -- which is exactly why the timestamp needs its own check. Without it,
    anyone who captured one valid request could resend it forever.
    """
    stale = str(int(TIMESTAMP) - MAX_TIMESTAMP_AGE_SECONDS - 1)

    assert verify(timestamp=stale, signature=sign(BODY, timestamp=stale)) is False


def test_a_timestamp_from_the_future_is_refused():
    """The window is absolute distance, not age.

    A request stamped an hour ahead is not more trustworthy than one an hour
    behind; a one-sided check would accept a captured request replayed by
    anyone able to stamp it forward.
    """
    ahead = str(int(TIMESTAMP) + MAX_TIMESTAMP_AGE_SECONDS + 1)

    assert verify(timestamp=ahead, signature=sign(BODY, timestamp=ahead)) is False


def test_the_edge_of_the_window_is_still_accepted():
    """Exactly five minutes old is inside the window Slack documents.

    Pinned so that a later change to the comparison -- `>` to `>=`, or a
    different unit -- is a failure here rather than an intermittent one in
    production against a server whose clock is a few seconds out.
    """
    edge = str(int(TIMESTAMP) - MAX_TIMESTAMP_AGE_SECONDS)

    assert verify(timestamp=edge, signature=sign(BODY, timestamp=edge)) is True


# --- headers that are not signatures at all ----------------------------


@pytest.mark.parametrize(
    "timestamp",
    ["not-a-number", "1700000000.5", "0x65", " 1700000000 ; DROP", "١٧٠٠٠٠٠٠٠٠"],
    ids=["text", "float", "hex", "injection-shaped", "non-ascii digits"],
)
def test_an_unparseable_timestamp_is_refused_without_raising(timestamp):
    """Every header here is attacker-controlled, so none may raise.

    The last case is the one that is easy to get wrong: Python's `int()`
    accepts non-ASCII digits, so a timestamp of "١٧٠٠٠٠٠٠٠٠" parses to a
    perfectly fresh instant -- and a naive implementation would then try to
    encode it as ASCII and raise. It has to fail the comparison instead, as a
    401 rather than a 500.
    """
    assert verify(timestamp=timestamp) is False


@pytest.mark.parametrize(
    "signature",
    ["v0=nonsense", "v1=" + "0" * 64, "café", "v0=" + "0" * 63, "v0=" + "0" * 65],
    ids=["wrong digest", "wrong version", "non-ascii", "too short", "too long"],
)
def test_a_malformed_signature_header_is_refused_without_raising(signature):
    """`hmac.compare_digest` refuses a non-ASCII str, so both sides are bytes.

    Without that, a header of "café" is a TypeError inside the verifier --
    which reaches the client as a 500 and tells an attacker they found a code
    path, rather than as the flat 401 every other bad signature gets.
    """
    assert verify(signature=signature) is False


def test_an_empty_body_is_signed_like_any_other():
    """A zero-length body has a signature too, and it is checked.

    Worth pinning because "no body" is the shape a probe sends, and a
    verifier that special-cased it would accept an unsigned POST.
    """
    assert verify(body=b"", signature=sign(b"")) is True
    assert verify(body=b"", signature=sign(BODY)) is False
