"""What the rate limiter is allowed to believe about who is calling.

`app/http_client_ip.py` produces the key for one of the two rate-limit
buckets, so every claim in it is a claim about whether an attacker can choose
their own budget. Three of them are asserted here and the first is the one
that matters:

  * X-Forwarded-For IS NOT READ. Behind `frontend/nginx.conf` that header is
    built with `$proxy_add_x_forwarded_for`, which APPENDS -- so its leftmost
    element, which is what the usual `split(",")[0]` idiom takes, is a string
    the client sent. A limiter keyed on it hands every request a fresh bucket
    to anyone who varies a header;
  * nothing that is not an address reaches a query. The column is capped by
    `auth_rate_limits_subject_shape`, so an unbounded or malformed value would
    be a CheckViolationError -- a 500 on the login path, at an unauthenticated
    caller's choosing -- rather than a rate limit;
  * one address is one bucket. IPv6 has many spellings of the same host, and
    two spellings would be two budgets.

Nothing here reaches a database or an event loop; these are functions over a
request object.
"""

import pytest
from starlette.datastructures import Headers

from app.http_client_ip import (
    MAX_ADDRESS_LENGTH,
    REAL_IP_HEADER,
    UNTRUSTED_FORWARDED_FOR_HEADER,
    read_client_ip,
)


CLIENT = "203.0.113.7"
PROXY = "198.51.100.1"


class FakeClient:
    """The `.client` attribute of a Starlette request: host and port."""

    def __init__(self, host: str):
        self.host = host
        self.port = 51000


class FakeRequest:
    """Enough of `starlette.requests.Request` for one header read and a peer.

    A stand-in rather than a real Request, because building one means
    constructing an ASGI scope, and every property under test here is reached
    through two attributes.
    """

    def __init__(self, headers: dict[str, str] | None = None, peer: str | None = None):
        self.headers = Headers(headers or {})
        self.client = FakeClient(peer) if peer is not None else None


def test_the_forwarded_for_header_is_never_read():
    """The forgery, spelled out as the attacker would send it.

    nginx appends its own view of the peer, so the header that arrives at the
    application is "<whatever the client claimed>, <the real address>". Taking
    the first element -- the ordinary idiom, and the reason this test is here
    rather than left implied -- would key the limiter on a value the caller
    chose, which is no limit at all.
    """
    request = FakeRequest(
        headers={
            UNTRUSTED_FORWARDED_FOR_HEADER: f"1.2.3.4, {CLIENT}",
            REAL_IP_HEADER: CLIENT,
        },
        peer=PROXY,
    )

    assert read_client_ip(request) == CLIENT


def test_a_forwarded_for_header_alone_yields_the_peer_and_not_the_claim():
    """With no X-Real-IP there is still nothing to learn from that header.

    A deployment reached without the nginx in front of it gets the TCP peer,
    which the caller cannot choose. What it must never get is the claim.
    """
    request = FakeRequest(
        headers={UNTRUSTED_FORWARDED_FOR_HEADER: "1.2.3.4"},
        peer=CLIENT,
    )

    assert read_client_ip(request) == CLIENT


def test_the_real_ip_header_wins_over_the_peer():
    """Behind the proxy the peer is the proxy, and the header is the caller.

    This is the one case that makes the header worth reading at all: nginx
    OVERWRITES X-Real-IP with `$remote_addr`, so what arrives is nginx's view
    of who connected rather than anything the client supplied.
    """
    request = FakeRequest(headers={REAL_IP_HEADER: CLIENT}, peer=PROXY)

    assert read_client_ip(request) == CLIENT


def test_the_peer_is_used_when_no_proxy_set_a_header():
    request = FakeRequest(peer=CLIENT)

    assert read_client_ip(request) == CLIENT


def test_no_request_is_no_address():
    """A context built by hand has no request, which is not an error."""
    assert read_client_ip(None) is None


def test_a_request_with_no_peer_is_no_address():
    """`request.client` is None over some transports, and None is the answer."""
    assert read_client_ip(FakeRequest()) is None


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "not-an-address",
        "999.999.999.999",
        "203.0.113.7; DROP TABLE auth_rate_limits",
        "203.0.113.7, 198.51.100.1",
        "<script>alert(1)</script>",
        "\x00",
    ],
)
def test_a_value_that_is_not_an_address_is_no_address(value):
    """Nothing that is not an address reaches a bucket key.

    The list is deliberately more than malformed numbers. `subject` is written
    into a query, and while every statement in the repository is parameterised,
    "it cannot be an injection because of the layer below" is not the same
    claim as "it never gets there". A value that parses as an IP address is the
    only value that gets there.
    """
    assert read_client_ip(FakeRequest(headers={REAL_IP_HEADER: value})) is None


def test_an_oversized_header_is_refused_before_it_can_violate_the_check():
    """`auth_rate_limits_subject_shape` caps the column at 128 characters.

    A scoped IPv6 address carries a zone id that `ipaddress` will accept at any
    length, so without the length guard an attacker could choose a subject
    longer than the column allows -- which is a CheckViolationError raised
    inside the log-in path on demand, a 500 rather than a refusal.
    """
    scoped = "fe80::1%" + "e" * 200

    assert len(scoped) > MAX_ADDRESS_LENGTH
    assert read_client_ip(FakeRequest(headers={REAL_IP_HEADER: scoped})) is None


def test_the_longest_real_address_still_fits():
    """The guard must not refuse an address somebody actually has.

    45 characters is the longest an address can be: a full IPv4-mapped IPv6
    form written out. A guard tuned one character short would silently drop
    the IP bucket for a whole class of clients.
    """
    longest = "0000:0000:0000:0000:0000:ffff:255.255.255.255"

    assert len(longest) == MAX_ADDRESS_LENGTH
    assert read_client_ip(FakeRequest(headers={REAL_IP_HEADER: longest})) is not None


@pytest.mark.parametrize(
    "spelling",
    [
        "2001:db8::1",
        "2001:0db8:0000:0000:0000:0000:0000:0001",
        "2001:DB8::1",
    ],
)
def test_one_address_spelled_several_ways_is_one_bucket(spelling):
    """Three spellings of one host must not be three budgets.

    IPv6 permits zero-compression, leading zeroes and either case, so a
    limiter keyed on the header text would let a caller triple their budget by
    typing the same address differently. Canonicalising through `ipaddress` is
    what collapses them.
    """
    assert read_client_ip(FakeRequest(headers={REAL_IP_HEADER: spelling})) == (
        "2001:db8::1"
    )


def test_surrounding_whitespace_does_not_make_a_second_bucket():
    assert read_client_ip(FakeRequest(headers={REAL_IP_HEADER: f"  {CLIENT} "})) == (
        CLIENT
    )
