"""What the rate limiter is allowed to believe about who is calling.

`app/http_client_ip.py` produces the key for one of the two rate-limit
buckets, so every claim in it is a claim about whether an attacker can choose
their own budget.

THE DEFECT THIS FILE NOW GUARDS. An earlier version read `X-Real-IP` and
trusted it, on the argument that `frontend/nginx.conf` sets that header with
`$remote_addr`, which OVERWRITES whatever the client sent. The argument was
correct and its premise was local: it held only where that nginx was the only
way in. Render terminates TLS at its own edge and forwards straight to the
application -- no nginx, nothing overwriting the header, and Render does not
strip it -- so on that deployment any caller could send `X-Real-IP:` and pick
which bucket their login attempts were counted in.

The `request.client` fallback was no better there. `render.yaml` sets
`FORWARDED_ALLOW_IPS: "*"` so that uvicorn honours `X-Forwarded-Proto` and
HSTS is sent at all; under `*` uvicorn's ProxyHeadersMiddleware trusts every
peer and takes the LEFTMOST `X-Forwarded-For` element -- the one the client
wrote -- into `scope["client"]`. Both paths were forgeable at once.

So the header is no longer chosen by this module. Whether a forwarding header
can be believed is a fact about the DEPLOYMENT, and the deployment states it
in `Settings.trusted_proxy_hops`: the number of proxies in front of this
process that are trusted to have APPENDED to `X-Forwarded-For`. The client is
that many elements from the right; everything to the left is hearsay.

The rest of the invariants are unchanged and still asserted: nothing that is
not an address reaches a query, and one address is one bucket.

Nothing here reaches a database or an event loop; these are functions over a
request object.
"""

import pytest
from starlette.datastructures import Headers

from app.http_client_ip import (
    FORWARDED_FOR_HEADER,
    MAX_ADDRESS_LENGTH,
    UNTRUSTED_REAL_IP_HEADER,
    read_client_ip,
)


CLIENT = "203.0.113.7"
PROXY = "198.51.100.1"
ATTACKER_CLAIM = "1.2.3.4"


class FakeClient:
    """The `.client` attribute of a Starlette request: host and port."""

    def __init__(self, host: str):
        self.host = host
        self.port = 51000


class FakeRequest:
    """Enough of `starlette.requests.Request` for header reads and a peer.

    A stand-in rather than a real Request, because building one means
    constructing an ASGI scope, and every property under test here is reached
    through two attributes.
    """

    def __init__(self, headers: dict[str, str] | None = None, peer: str | None = None):
        self.headers = Headers(headers or {})
        self.client = FakeClient(peer) if peer is not None else None


# --- the spoofing regressions -----------------------------------------------


@pytest.mark.parametrize("hops", [0, 1, 2])
def test_a_spoofed_real_ip_header_is_never_read(hops):
    """X-Real-IP is ignored at every hop count, including behind a proxy.

    THE RENDER DEFECT, as the attacker would send it. There is no nginx in
    front, so nothing rewrites this header and it arrives exactly as typed. If
    it were still read, every request could name its own bucket.

    Asserted across hop counts because "we only trust it behind a proxy" is
    the shape the original bug had -- the module believed it was behind one.
    """
    request = FakeRequest(
        headers={
            UNTRUSTED_REAL_IP_HEADER: ATTACKER_CLAIM,
            FORWARDED_FOR_HEADER: CLIENT,
        },
        peer=PROXY,
    )

    assert read_client_ip(request, trusted_hops=hops) != ATTACKER_CLAIM


def test_a_real_ip_header_alone_does_not_become_the_bucket():
    """With nothing else to go on, the answer is the peer -- never the claim."""
    request = FakeRequest(
        headers={UNTRUSTED_REAL_IP_HEADER: ATTACKER_CLAIM},
        peer=CLIENT,
    )

    assert read_client_ip(request, trusted_hops=0) == CLIENT


def test_padding_the_forwarded_for_header_cannot_reach_the_trusted_position():
    """The other half of the forgery: write the list yourself.

    A caller sending `X-Forwarded-For: 1.2.3.4` has the proxy APPEND to it, so
    what arrives is "1.2.3.4, <the real address>". Reading from the left --
    the ordinary `split(",")[0]` idiom, and what uvicorn does under
    `forwarded_allow_ips="*"` -- takes the attacker's value.

    Reading from the right cannot: every element the caller adds pushes their
    own values FURTHER from the position that is read. Three claims here, and
    the trusted element is still the one the proxy wrote.
    """
    request = FakeRequest(
        headers={
            FORWARDED_FOR_HEADER: f"{ATTACKER_CLAIM}, 5.6.7.8, 9.10.11.12, {CLIENT}"
        },
        peer=PROXY,
    )

    assert read_client_ip(request, trusted_hops=1) == CLIENT


def test_a_caller_cannot_spoof_by_claiming_the_proxys_own_position():
    """Even a list built to look like a two-hop chain yields one trusted hop.

    With `trusted_proxy_hops=1` the index is fixed by configuration, not
    derived from how long the caller made the list. So a caller who sends two
    addresses hoping to be read as "proxy, client" still only moves themselves
    left.
    """
    request = FakeRequest(
        headers={FORWARDED_FOR_HEADER: f"{ATTACKER_CLAIM}, {ATTACKER_CLAIM}, {CLIENT}"},
        peer=PROXY,
    )

    assert read_client_ip(request, trusted_hops=1) == CLIENT


def test_no_trusted_proxy_means_the_forwarded_header_is_not_read_at_all():
    """hops=0 is the default, and the default must not read a caller's header.

    A deployment reached directly has nothing appending to this header, so
    every element in it was written by whoever is calling.
    """
    request = FakeRequest(
        headers={FORWARDED_FOR_HEADER: f"{ATTACKER_CLAIM}, {ATTACKER_CLAIM}"},
        peer=CLIENT,
    )

    assert read_client_ip(request, trusted_hops=0) == CLIENT


# --- reading the header where it IS trustworthy ------------------------------


def test_one_trusted_hop_reads_the_address_the_proxy_appended():
    """Behind nginx or Render's edge the peer is the proxy and the last element
    is the address that proxy accepted the connection from."""
    request = FakeRequest(
        headers={FORWARDED_FOR_HEADER: f"{ATTACKER_CLAIM}, {CLIENT}"},
        peer=PROXY,
    )

    assert read_client_ip(request, trusted_hops=1) == CLIENT


def test_two_trusted_hops_read_the_second_element_from_the_right():
    """Two appending proxies -- an ingress in front of nginx, say -- put the
    client one position further left, and the count says so."""
    request = FakeRequest(
        headers={FORWARDED_FOR_HEADER: f"{ATTACKER_CLAIM}, {CLIENT}, {PROXY}"},
        peer=PROXY,
    )

    assert read_client_ip(request, trusted_hops=2) == CLIENT


def test_a_list_shorter_than_the_hop_count_is_refused():
    """The request did not come through the proxies this deployment believes
    are in front of it, so no element of the list is trustworthy.

    Refusing is the safe answer and losing the bucket is the cost. Guessing
    the leftmost element instead would be exactly the bug.
    """
    request = FakeRequest(headers={FORWARDED_FOR_HEADER: CLIENT}, peer=PROXY)

    assert read_client_ip(request, trusted_hops=2) is None


def test_a_missing_forwarded_header_behind_a_proxy_is_refused():
    """Configured for a proxy and reached without one: nothing to read.

    Not the peer, deliberately. With hops>0 the peer is the proxy, and keying
    every such request on the proxy's address would put the whole internet in
    one bucket the moment the header went missing.
    """
    request = FakeRequest(peer=PROXY)

    assert read_client_ip(request, trusted_hops=1) is None


def test_the_peer_is_used_when_no_proxy_is_trusted():
    assert read_client_ip(FakeRequest(peer=CLIENT), trusted_hops=0) == CLIENT


def test_the_peer_is_refused_when_uvicorn_may_have_rewritten_it():
    """Belt to the hop count's braces, and the case that would have stayed open.

    `TRUSTED_PROXY_HOPS` is set in render.yaml, but a blueprint env change does
    not always reach a running service. If it did not, `trusted_hops` would be
    0 here -- and on Render `request.client` is NOT the TCP peer, because
    `FORWARDED_ALLOW_IPS: "*"` makes uvicorn overwrite it with the leftmost
    `X-Forwarded-For` element, which the caller wrote. The fallback would have
    handed back the forgery the hop count was added to prevent.

    So when the peer cannot be believed the answer is None: no IP bucket at
    all, with the per-address budget and the argon2 semaphore untouched. Giving
    up a backstop beats keying it on a value the limited party chose.
    """
    request = FakeRequest(peer=ATTACKER_CLAIM)

    assert read_client_ip(request, trusted_hops=0, peer_is_trustworthy=False) is None


def test_a_trusted_hop_count_still_works_when_the_peer_is_untrustworthy():
    """The two settings are independent, and the header path does not care.

    Render is exactly this pair: `FORWARDED_ALLOW_IPS: "*"` (so the peer is
    meaningless) and one appending proxy (so the last element is real).
    """
    request = FakeRequest(
        headers={FORWARDED_FOR_HEADER: f"{ATTACKER_CLAIM}, {CLIENT}"},
        peer=ATTACKER_CLAIM,
    )

    assert read_client_ip(request, trusted_hops=1, peer_is_trustworthy=False) == CLIENT


def test_no_request_is_no_address():
    """A context built by hand has no request, which is not an error."""
    assert read_client_ip(None, trusted_hops=0) is None


def test_a_request_with_no_peer_is_no_address():
    """`request.client` is None over some transports, and None is the answer."""
    assert read_client_ip(FakeRequest(), trusted_hops=0) is None


# --- what may reach a bucket key ---------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "not-an-address",
        "999.999.999.999",
        "203.0.113.7; DROP TABLE auth_rate_limits",
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
    request = FakeRequest(headers={FORWARDED_FOR_HEADER: value})

    assert read_client_ip(request, trusted_hops=1) is None


def test_an_oversized_element_is_refused_before_it_can_violate_the_check():
    """`auth_rate_limits_subject_shape` caps the column at 128 characters.

    A scoped IPv6 address carries a zone id that `ipaddress` will accept at any
    length, so without the length guard an attacker could choose a subject
    longer than the column allows -- which is a CheckViolationError raised
    inside the log-in path on demand, a 500 rather than a refusal.
    """
    scoped = "fe80::1%" + "e" * 200

    assert len(scoped) > MAX_ADDRESS_LENGTH

    request = FakeRequest(headers={FORWARDED_FOR_HEADER: scoped})

    assert read_client_ip(request, trusted_hops=1) is None


def test_the_longest_real_address_still_fits():
    """The guard must not refuse an address somebody actually has.

    45 characters is the longest an address can be: a full IPv4-mapped IPv6
    form written out. A guard tuned one character short would silently drop
    the IP bucket for a whole class of clients.
    """
    longest = "0000:0000:0000:0000:0000:ffff:255.255.255.255"

    assert len(longest) == MAX_ADDRESS_LENGTH

    request = FakeRequest(headers={FORWARDED_FOR_HEADER: longest})

    assert read_client_ip(request, trusted_hops=1) is not None


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
    request = FakeRequest(headers={FORWARDED_FOR_HEADER: spelling})

    assert read_client_ip(request, trusted_hops=1) == "2001:db8::1"


def test_surrounding_whitespace_does_not_make_a_second_bucket():
    """The header is written with ", " between elements, so every element
    after the first arrives with a leading space."""
    request = FakeRequest(
        headers={FORWARDED_FOR_HEADER: f"{ATTACKER_CLAIM},  {CLIENT} "}
    )

    assert read_client_ip(request, trusted_hops=1) == CLIENT
