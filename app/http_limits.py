"""Size limits enforced at the HTTP edge, before anything parses a request."""

from fastapi import FastAPI
from starlette.middleware.body_limit import RequestBodyLimitMiddleware


# A GraphQL document, its variables and a JSON envelope. Generous for the
# largest query the product plausibly sends, and small enough that an
# attacker cannot make the process pay for a body it will refuse anyway.
MAX_REQUEST_BODY_BYTES = 256 * 1024


def add_request_body_limit(app: FastAPI) -> None:
    """Bound the request body for every route, not just the GraphQL mount.

    Scoping the limit to /graphql would protect exactly today's routes and
    silently leave tomorrow's unprotected: every webhook, upload or REST
    endpoint added later would default to unbounded, and nothing would
    report that. Wrapping the application makes bounded the default and an
    explicit override the exception -- starlette carries the ceiling in the
    ASGI scope, so a route that genuinely needs a larger body can raise its
    own without loosening this one.

    starlette's middleware is used rather than a local one because getting
    this right means handling two different lies. A declared Content-Length
    over the limit is refused before a single byte of the body is read. A
    body that overruns whatever it declared -- an understated header, or a
    chunked request that declared nothing at all -- is caught by counting
    bytes as they arrive and aborting the moment the total passes the
    limit, so the process never holds more than the limit plus one chunk.
    Rolling that by hand means re-implementing the ASGI receive channel,
    where the classic bug is consuming the stream to measure it and handing
    the application an empty body.
    """
    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_body_size=MAX_REQUEST_BODY_BYTES,
    )
