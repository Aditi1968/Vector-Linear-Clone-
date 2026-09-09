"""Response headers that harden every route, set at the HTTP edge.

Four headers, and the reasoning for each is below rather than in a list,
because the ones that are safe everywhere and the ones that depend on how
this application is deployed are not the same set.

There were none before this. Neither the application nor `frontend/nginx.conf`
set a single security header -- nginx set only `Cache-Control` -- so every
response, API and page alike, went out with browser defaults. That is not a
vulnerability by itself; it removes the cheap defences that limit what a bug
elsewhere can become.

Set on the application rather than on nginx, because nginx is not always in
front of it. `k8s/30-api.yaml` publishes the api Service as a ClusterIP that
anything in the namespace can reach directly, `docker-compose.yml` publishes
it on a host port, and `run-vector-local.ps1` serves it with no proxy at all.
A header set only in the proxy is a header absent from three of the four ways
this application is actually reached. nginx keeps its own for the static
bundle, which needs a different, looser CSP than a JSON API does.
"""

from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response


# What a JSON API is allowed to pull in: nothing.
#
# Correct here precisely because it would be wrong for the frontend. Every
# response this application returns is JSON -- GraphQL payloads and the two
# health endpoints -- and JSON loads no scripts, styles, images or fonts. So
# the honest policy is a refusal of all of them, and it costs nothing because
# nothing is being refused in practice.
#
# `frame-ancestors 'none'` is the load-bearing half and is the reason this is
# a CSP rather than three simpler headers. It is what refuses framing, and it
# supersedes X-Frame-Options in every browser that reads both -- so
# X-Frame-Options is deliberately NOT set as well, rather than set to a value
# a modern browser ignores and a reader mistakes for the protection.
#
# GraphiQL is the one exception and it is handled below: in development the
# /graphql route serves a real HTML page with inline script and style, and
# this policy would blank it.
API_CONTENT_SECURITY_POLICY = "default-src 'none'; frame-ancestors 'none'"

# The same policy with the two allowances GraphiQL needs, and nothing else.
# `unsafe-inline` is normally a smell; here the document being protected is a
# developer tool that only exists when ENVIRONMENT is not production, and the
# alternative is either no CSP on that route or a nonce threaded through a
# page this application does not render itself.
GRAPHIQL_CONTENT_SECURITY_POLICY = (
    "default-src 'none'; frame-ancestors 'none'; "
    "script-src 'self' 'unsafe-inline' https://unpkg.com; "
    "style-src 'self' 'unsafe-inline' https://unpkg.com; "
    "img-src 'self' data:; font-src 'self' data:; connect-src 'self'"
)

# Referrer policy, chosen for what it does to OAuth rather than for strictness.
#
# `no-referrer` would be stricter and is the wrong call: an OAuth provider
# that cannot see which origin sent a user is a provider that cannot enforce
# its own redirect allowlist, and some refuse the request outright.
# `strict-origin-when-cross-origin` sends the full URL within this origin, the
# bare origin when crossing to another HTTPS one, and nothing at all when
# downgrading to HTTP -- so a GitHub or Slack callback still knows who sent it
# while a query string carrying an invitation token never leaves the origin.
REFERRER_POLICY = "strict-origin-when-cross-origin"

# Browser features this application never uses, denied so that a compromised
# page cannot ask for them. Listed explicitly rather than as a wildcard,
# because the header has no wildcard and an empty allowlist per feature is
# what "deny" is spelled as.
PERMISSIONS_POLICY = (
    "accelerometer=(), autoplay=(), camera=(), display-capture=(), "
    "encrypted-media=(), fullscreen=(self), geolocation=(), gyroscope=(), "
    "magnetometer=(), microphone=(), midi=(), payment=(), usb=()"
)


def add_security_headers(app: FastAPI) -> None:
    """Attach the headers to every response, including error responses.

    A middleware rather than a dependency, so that a 422 from validation, a
    500 from an unhandled exception and a 404 from no route at all carry them
    too. Those are exactly the responses a dependency would miss, and an
    error page is not less worth protecting than a successful one.

    Nothing here overwrites a header a route already set. That matters for
    one route today -- the GraphiQL page needs its own CSP -- and it keeps
    this middleware from being the reason a future route cannot make its own
    decision.
    """

    @app.middleware("http")
    async def _security_headers(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)

        # GraphiQL is served from the GraphQL route as HTML, and only when the
        # environment allows it. Detected from the response's own content type
        # rather than from the path or the settings: the route decides whether
        # to serve the tool, and reading the answer off the response is what
        # keeps this middleware from having to know that rule twice.
        is_html = response.headers.get("content-type", "").startswith("text/html")

        defaults = {
            "Content-Security-Policy": (
                GRAPHIQL_CONTENT_SECURITY_POLICY
                if is_html
                else API_CONTENT_SECURITY_POLICY
            ),
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": REFERRER_POLICY,
            "Permissions-Policy": PERMISSIONS_POLICY,
        }

        for header, value in defaults.items():
            response.headers.setdefault(header, value)

        # HSTS only on a request that actually arrived over TLS.
        #
        # Sent unconditionally it would be either useless or harmful: a
        # browser ignores it on a plain-HTTP response, and a developer who
        # once loaded http://localhost would be pinned to HTTPS for
        # localhost until the header's max-age expired -- with no HTTPS
        # listener there to satisfy it. `request.url.scheme` reflects
        # X-Forwarded-Proto when the proxy sets it, which is what makes this
        # correct behind nginx as well as in front of it.
        #
        # No `includeSubDomains` and no `preload`: both are commitments about
        # names this application does not own, and neither is reversible on
        # the timescale of the max-age.
        if request.url.scheme == "https":
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")

        return response
