"""Who a request came from, to the extent that anything here can know.

This exists for one caller -- the rate limiter's IP bucket in
`app.services.auth` -- and the honest answer it gives is "an address, or
nothing". Both halves of that matter, so the reasoning is here rather than
spread across the two resolvers that use it.


WHY X-Real-IP AND NOT X-Forwarded-For

`frontend/nginx.conf` sets both, and it sets them differently:

    proxy_set_header X-Real-IP        $remote_addr;
    proxy_set_header X-Forwarded-For  $proxy_add_x_forwarded_for;

The first OVERWRITES: whatever a client sent under that name is discarded and
replaced with the peer address nginx actually accepted the connection from.
The second APPENDS -- that is what `$proxy_add_x_forwarded_for` means -- so
its leftmost element is a string the client chose, preserved verbatim.

The usual idiom is `X-Forwarded-For.split(",")[0]`, and behind this proxy that
idiom reads an attacker-supplied value. A client that sets
`X-Forwarded-For: 1.2.3.4` gets `1.2.3.4, <their real address>` here, and a
limiter keyed on the first element would hand every request its own bucket by
sending a different number each time. So this file does not read that header
at all, and the constant below is named only to say so.


WHY THE ANSWER IS STILL ONLY A BACKSTOP

Unforgeable is not the same as accurate, and `X-Real-IP` is the first without
being the second. `$remote_addr` is whoever opened the TCP connection to the
nginx pod, which in the shapes this repository actually deploys is very often
not the user:

  * k8s/40-web.yaml publishes `web` as a NodePort Service. The default
    `externalTrafficPolicy: Cluster` SNATs, so `$remote_addr` is a node
    address and every external client collapses onto it;
  * that manifest's own comment says a real deployment fronts this with an
    Ingress or a LoadBalancer, and unless that hop is configured to preserve
    the client address, `$remote_addr` is the ingress controller's.

A collapsed address is why `app.services.auth` treats the IP budget as the
loose backstop and the per-address budget as the real limit: a tight budget on
a key that might be one value for the whole internet is a self-inflicted
outage, not a rate limit.

  ponytail: no trusted-proxy allowlist and no PROXY protocol. Both are the
  real fix for accuracy, and both are configuration in a component this
  repository does not own -- the upgrade path is `externalTrafficPolicy:
  Local` on the web Service, or an ingress that sets the client address, and
  then this function's answer becomes precise with no change here.


WHAT IS AND IS NOT FORGEABLE

k8s/30-api.yaml publishes `api` as ClusterIP, and docker-compose.yml binds it
to 127.0.0.1, so nothing outside the cluster or off the host reaches the
application except through nginx -- which overwrites the header. An external
attacker cannot choose their bucket. Anything already inside the cluster can,
and that is accepted: it evades a backstop, not the per-address budget and not
the semaphore in `app.services.passwords`.
"""

import ipaddress

from starlette.requests import Request


# Set by frontend/nginx.conf with $remote_addr, which overwrites rather than
# appends. That is the only reason this header is readable at all.
REAL_IP_HEADER = "x-real-ip"

# Deliberately never read; see the module docstring. Named so that the next
# person to reach for the obvious header finds the argument against it instead
# of the header.
UNTRUSTED_FORWARDED_FOR_HEADER = "x-forwarded-for"

# The longest an address can be and still be one: 45 characters is a full
# IPv4-mapped IPv6 form ("0000:...:0000:255.255.255.255"). Checked BEFORE
# parsing, because `ipaddress` accepts a scoped address whose zone id is
# arbitrarily long -- and `auth_rate_limits_subject_shape` caps the column at
# 128, so an unbounded value here is a CheckViolationError raised at an
# unauthenticated caller's choosing rather than a rate limit.
MAX_ADDRESS_LENGTH = 45


def read_client_ip(request: Request | None) -> str | None:
    """The caller's address, canonicalised; or None if there is not one.

    None is a real answer and not a failure. A context built by hand has no
    request, a deployment may be reached in a shape that sets no header, and a
    header that does not parse as an address is not one. The caller's job is
    to carry on with the budget it can still enforce -- see
    `AuthService._login_buckets` -- because the alternative is a login path
    that stops working when a proxy configuration changes.

    Canonicalised through `ipaddress` rather than trusted as text, which does
    three jobs in one call: it rejects anything that is not an address, so no
    header value reaches a query; it collapses the several spellings of one
    IPv6 address into one bucket, so `::1` and `0:0:0:0:0:0:0:1` cannot be two
    budgets; and it guarantees the result fits the column.
    """
    if request is None:
        return None

    forwarded = request.headers.get(REAL_IP_HEADER)

    if forwarded is not None:
        return _address(forwarded)

    # The TCP peer, for a deployment reached without the nginx in front of it
    # -- `uvicorn` in development, a test client, a probe inside the cluster.
    # Never forgeable, frequently the proxy rather than the user, which is the
    # same tradeoff the header carries and the reason neither is the primary
    # key.
    if request.client is None:
        return None

    return _address(request.client.host)


def _address(value: str) -> str | None:
    """`value` as a canonical address, or None if it is not one."""
    text = value.strip()

    if not text or len(text) > MAX_ADDRESS_LENGTH:
        return None

    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        # The only thing a malformed address means is that this request has no
        # address to key on. Not an error: `X-Real-IP` is set by a component
        # outside this repository, and a proxy that starts sending something
        # else must not take the login path down with it.
        return None
