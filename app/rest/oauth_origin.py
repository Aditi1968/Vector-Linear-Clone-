"""Where an OAuth flow has to begin, so that its state survives the trip.

A state cookie is written by the start leg and read by the callback, and a
browser sends a cookie back only to the HOST that set it -- RFC 6265 host
matching, which no cookie attribute can widen. The callback's host is not
this application's to choose: it is whatever URL the provider has registered.
So when the two differ, the start leg is the half that has to move.

That is not hypothetical. In development the app is browsed on
`http://localhost:5173` while both providers redirect to a public tunnel
host, so the state was minted on `localhost`, the callback arrived on the
tunnel, and the cookie it needed had never existed there. GitHub and Slack
failed identically, and both refusals were correct: nothing was wrong with
the check, the state was written somewhere the callback could not read it.

The session cookie is the proof it is only about the host. It carries exactly
the same policy -- `SameSite=Lax`, `Path=/`, not Secure in development -- and
it arrives on that cross-site callback perfectly well, because the browser
was signed in on the tunnel host. Same attributes, different origin, opposite
outcome.
"""

from urllib.parse import urlsplit, urlunsplit

from fastapi import status
from starlette.requests import Request
from starlette.responses import RedirectResponse


def redirect_to_callback_origin(
    request: Request,
    callback_url: str | None,
) -> RedirectResponse | None:
    """This same start URL on the callback's host, or None if already there.

    Compared on hostname alone, because that is what a cookie jar is keyed
    on. A flow starting on `localhost:5173` with a callback on
    `localhost:8000` shares a jar and needs no hop -- cookies ignore both the
    port and the scheme -- while one starting on `localhost` with a callback
    on a tunnel host does not.

    None when nothing is configured, which is a real state: an app whose
    callback lands on the origin it is served from has nothing to correct,
    and a deployment that has not been told its callback URL has nothing to
    correct it towards.

    Not an open redirect. The scheme and host come from configuration and are
    identical for every caller; only this route's own path and query are
    carried across, and there is no parameter here anyone can aim.

    ponytail: one hop, no loop guard. A proxy that rewrote Host to something
    other than the configured callback host would redirect forever -- the
    deployed nginx passes `Host $host` through, and a loop is loud. A marker
    parameter is the fix if one ever shows up.
    """
    if not callback_url:
        return None

    target = urlsplit(callback_url)

    if not target.scheme or not target.hostname:
        return None

    if target.hostname == request.url.hostname:
        return None

    return RedirectResponse(
        urlunsplit(
            (
                target.scheme,
                target.netloc,
                request.url.path,
                request.url.query,
                "",
            )
        ),
        status_code=status.HTTP_302_FOUND,
    )
