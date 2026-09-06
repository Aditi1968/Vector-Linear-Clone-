"""The session cookie: one name, one policy, three functions.

Everything about how the raw session token travels between the browser and
this server is decided here and nowhere else. Scattering `set_cookie` calls
through resolvers is how a log-out ends up clearing a cookie under a path
the log-in never wrote it to, leaving the browser holding a token the server
believes it revoked.

Why a cookie rather than an Authorization header the client stores itself:
`HttpOnly` is only available to a cookie, and it is the difference between
a cross-site scripting bug that reads the DOM and one that walks away with a
session token. The cost of choosing cookies is CSRF, which is what
`SameSite` below addresses.
"""

from typing import Literal

from starlette.requests import HTTPConnection
from starlette.responses import Response

from app.config import Environment


# Deliberately one name in every environment.
#
# The stronger choice is the `__Host-` prefix, which browsers refuse to
# accept unless the cookie is Secure, path=/ and carries no Domain -- so a
# compromised sibling subdomain cannot set a cookie the main site will read.
# It cannot be used here without splitting the name by environment, because
# Secure is exactly what development over http cannot set, and then every
# read has to know which environment it is in to know what to look for. That
# is a change to make when there is a real domain to make it against.
SESSION_COOKIE_NAME = "vector_session"

# Sent with every request to this origin. Narrower would mean the cookie is
# absent from any route outside the prefix, and the GraphQL endpoint is not
# the only thing that will ever need to know who is calling.
SESSION_COOKIE_PATH = "/"

# Lax, not Strict, and not None.
#
# Lax withholds the cookie from cross-site POSTs -- which is every
# interesting CSRF -- while still sending it on a top-level navigation, so
# following a link to the app from an email arrives signed in. Strict breaks
# that and buys little for a first-party single-page application. None would
# send the cookie on every cross-site request there is, and is only for
# deployments that are deliberately embedded in third-party pages.
#
# Annotated as a Literal, not left as `str`: starlette's set_cookie takes a
# closed set of values here, and a plain string constant would let a typo
# through to a header the browser quietly ignores.
SESSION_COOKIE_SAMESITE: Literal["lax"] = "lax"


def is_secure_environment(environment: Environment) -> bool:
    """Whether the cookie may be marked Secure.

    Only production, because Secure means "never send this over plain http"
    and development and test are plain http. Setting it everywhere would not
    be safer, it would be a cookie the browser silently refuses to store
    locally, which reads as authentication that does not work.
    """
    return environment == "production"


def read_session_token(connection: HTTPConnection) -> str | None:
    """The raw token the caller presented, if any.

    Typed against HTTPConnection rather than Request so that it also accepts
    the WebSocket Strawberry can hand a context -- both carry parsed cookies,
    and a subscription needs the same identity an HTTP request does.
    """
    return connection.cookies.get(SESSION_COOKIE_NAME)


def set_session_cookie(
    response: Response,
    *,
    token: str,
    max_age: int,
    environment: Environment,
) -> None:
    """Hand the browser a freshly issued session token.

    `max_age` is a hint and nothing more: it tells the browser when to stop
    sending the token, while the server decides whether a token still works
    by comparing `sessions.expires_at` against its own clock. A client that
    ignores it gains nothing.

    Max-Age rather than Expires because Expires is an absolute instant
    interpreted against the *browser's* clock, and a machine whose clock is
    days out would either drop a live session or hold a dead one.
    """
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=max_age,
        path=SESSION_COOKIE_PATH,
        httponly=True,
        secure=is_secure_environment(environment),
        samesite=SESSION_COOKIE_SAMESITE,
    )


def clear_session_cookie(response: Response, *, environment: Environment) -> None:
    """Remove the session cookie from the browser.

    Every attribute has to match the one `set_session_cookie` wrote, or the
    browser treats this as a different cookie and keeps the original -- which
    would leave a client sending a token on every request that the server has
    already deleted. That is the whole reason both functions live in one file
    reading from the same constants.
    """
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path=SESSION_COOKIE_PATH,
        httponly=True,
        secure=is_secure_environment(environment),
        samesite=SESSION_COOKIE_SAMESITE,
    )
