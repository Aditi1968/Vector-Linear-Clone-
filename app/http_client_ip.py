"""Who a request came from, to the extent that anything here can know.

This exists for one caller -- the rate limiter's IP bucket in
`app.services.auth` -- and the honest answer it gives is "an address, or
nothing". Both halves of that matter, so the reasoning is here rather than
spread across the two resolvers that use it.


WHY A HOP COUNT, AND NOT A HEADER THIS MODULE PICKS

An earlier version of this file read `X-Real-IP` and argued at length that it
was the safe one, because `frontend/nginx.conf` sets it with `$remote_addr`,
which OVERWRITES whatever the client sent. That was true, and it was true only
behind that nginx.

The first deployment without an nginx -- Render, which terminates TLS at its
edge and forwards straight to this process -- made the header forgeable, and
nothing in the code changed or could have noticed. That is the actual lesson
and it is why the answer is now configuration: whether a header can be trusted
is a fact about the DEPLOYMENT, so the deployment states it, in
`Settings.trusted_proxy_hops`.

`X-Forwarded-For` is a list every proxy APPENDS to. Its leftmost element is
whatever the client typed; its rightmost was written by the proxy nearest this
process. So with N trusted hops the client is the Nth element from the right,
and everything left of that is hearsay. Both shapes this repository deploys
append exactly once -- nginx's `$proxy_add_x_forwarded_for`, and Render's edge
-- so both are `trusted_proxy_hops=1`, reading the same value nginx used to
put in `X-Real-IP`.

The common idiom is `X-Forwarded-For.split(",")[0]`, which reads the element
the client chose. It is what uvicorn's own ProxyHeadersMiddleware does when
`--forwarded-allow-ips` is `*`, which is why `request.client` is not a safe
fallback on such a deployment either -- see `read_client_ip`.


WHY THE ANSWER IS STILL ONLY A BACKSTOP

Unforgeable is not the same as accurate, and the trusted element is the first
without being the second. What a proxy appends is whoever opened the TCP
connection TO THAT PROXY, which in the shapes this repository actually deploys
is very often not the user:

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

  ponytail: a hop COUNT, not a trusted-proxy address allowlist, and no PROXY
  protocol. A count is right when the proxies in front are fixed, which is
  true of every deployment here; an allowlist of proxy addresses is what a
  variable or multi-tenant chain needs. The accuracy ceiling above is not
  fixed by either -- the upgrade path is still `externalTrafficPolicy: Local`
  on the web Service, or an ingress that preserves the client address, and
  then this function's answer becomes precise with no change here.


WHAT IS AND IS NOT FORGEABLE

With `trusted_proxy_hops` set correctly, a caller cannot choose their bucket
from outside: padding the header with their own entries only pushes those
entries further from the position that is read. Getting the number too high is
the way to break that, which is why it is a small explicit integer per
deployment rather than "trust the chain".

k8s/30-api.yaml publishes `api` as ClusterIP and docker-compose.yml binds it
to 127.0.0.1, so in those shapes nothing reaches the application except
through nginx. Anything already inside the cluster can still forge, and that
is accepted: it evades a backstop, not the per-address budget and not the
semaphore in `app.services.passwords`.
"""

import ipaddress

from starlette.requests import Request


# The list each proxy APPENDS to, read from the right under an explicit hop
# count. See `_from_forwarded_for`.
FORWARDED_FOR_HEADER = "x-forwarded-for"

# Deliberately never read, and the reversal of an earlier decision that this
# module was built around.
#
# `frontend/nginx.conf` sets this header with `$remote_addr`, which OVERWRITES
# whatever the client sent -- so behind that nginx it was unforgeable, and
# this module read it for that reason. The argument was sound and its premise
# was local: it held only where nginx was the sole way in.
#
# Render terminates TLS at its own edge and forwards straight to this process.
# There is no nginx, nothing overwrites this header, and Render does not strip
# it -- so a caller could send `X-Real-IP: <anything>` and choose which
# rate-limit bucket their login attempts were counted in. The header is
# untrustworthy unless something is known to be rewriting it, and "something
# is rewriting it" is not a fact this process can check at runtime.
#
# `X-Forwarded-For` with a configured hop count replaces it and covers both
# shapes: nginx's `$proxy_add_x_forwarded_for` appends, Render's edge appends,
# and in both cases the rightmost element is the address that proxy accepted
# the connection from -- the same value nginx was putting here.
UNTRUSTED_REAL_IP_HEADER = "x-real-ip"

# The longest an address can be and still be one: 45 characters is a full
# IPv4-mapped IPv6 form ("0000:...:0000:255.255.255.255"). Checked BEFORE
# parsing, because `ipaddress` accepts a scoped address whose zone id is
# arbitrarily long -- and `auth_rate_limits_subject_shape` caps the column at
# 128, so an unbounded value here is a CheckViolationError raised at an
# unauthenticated caller's choosing rather than a rate limit.
MAX_ADDRESS_LENGTH = 45


def read_client_ip(
    request: Request | None,
    *,
    trusted_hops: int,
    peer_is_trustworthy: bool = True,
) -> str | None:
    """The caller's address, canonicalised; or None if there is not one.

    `trusted_hops` is how many proxies in front of this process are trusted to
    have appended to `X-Forwarded-For` -- `Settings.trusted_proxy_hops`. It is
    a required keyword argument on purpose: the old signature took a request
    and nothing else, which meant every caller got the DEPLOYMENT-SHAPED
    decision made for them by a default, and the default was wrong on the
    first deployment that had no nginx in front of it.

    `peer_is_trustworthy` is `Settings.peer_address_is_trustworthy`, and it
    exists because "fall back to the peer" is not automatically safe. Under
    `--forwarded-allow-ips=*` uvicorn overwrites `scope["client"]` with the
    LEFTMOST `X-Forwarded-For` element for any peer, so `request.client` is a
    caller-supplied value there. This is the belt to the hop count's braces:
    it keeps the fallback safe on a deployment that set `*` for HSTS and never
    set a hop count, which is precisely the configuration this fix would
    otherwise leave exposed.

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

    if trusted_hops > 0:
        return _from_forwarded_for(
            request.headers.get(FORWARDED_FOR_HEADER), trusted_hops
        )

    # No trusted proxy. The fallback is the address the socket was opened
    # from -- but only where that is still what `request.client` means.
    #
    # Under `--forwarded-allow-ips=*` it is not: uvicorn has already replaced
    # it with the leftmost `X-Forwarded-For` element, which the caller wrote.
    # Refusing here gives up the IP bucket and keeps the per-address budget
    # and the argon2 semaphore, which is strictly better than keying a limit
    # on a value the person being limited chose.
    if not peer_is_trustworthy:
        return None

    if request.client is None:
        return None

    return _address(request.client.host)


def _from_forwarded_for(header: str | None, trusted_hops: int) -> str | None:
    """The Nth element from the RIGHT of `X-Forwarded-For`, or None.

    From the right because that is the direction the header is written in:
    each proxy appends the address it accepted the connection from, so the
    last element was written by the proxy nearest this process and the first
    is whatever the client typed. Counting from the left -- the common idiom,
    and what uvicorn does under `forwarded_allow_ips="*"` -- reads
    attacker-supplied text.

    A client CAN pad the header: sending three addresses of their own shifts
    everything right by three. That is exactly why the index is counted from
    the end and fixed by configuration rather than derived from the list's
    length -- padding moves the attacker's values further from the trusted
    position, never into it.

    Refuses when the list is shorter than the configured hop count. That means
    the request did not pass through the proxies this deployment believes are
    in front of it, so no element of it is trustworthy; there is no reading of
    a too-short list that is safe to guess at.
    """
    if not header:
        return None

    parts = [element.strip() for element in header.split(",")]
    parts = [element for element in parts if element]

    if len(parts) < trusted_hops:
        return None

    return _address(parts[-trusted_hops])


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
