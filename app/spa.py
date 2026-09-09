"""The built frontend, served by this application, on the same origin.

WHY THE API SERVES THE PAGE. Not for tidiness: this application authenticates
with an opaque database-backed session cookie and mints OAuth state cookies,
and a browser sends a cookie back only to the HOST that set it. When the page
and the API are two hosts, a state cookie written by the start leg does not
exist at the callback -- which is not hypothetical, it is the failure
`app/rest/oauth_origin.py` was written to correct. One origin removes the
condition entirely: there is no second host for a cookie to be missing from,
no CORS to configure, no `SameSite=None` to weaken, and `graphqlUrl` in
`frontend/src/lib/config/env.ts` stays the relative path it already defaults
to.

`frontend/nginx.conf` does the same job for the compose and Kubernetes stacks
by proxying. This is that arrangement without the second container, which is
what a single free-tier web service can actually run.

WHY `static/` AND NOT `frontend/dist/`. The bundle is copied into the image at
the repository root as `static/`, a directory that exists in no developer
checkout. `frontend/dist/` would have been the natural name and is the wrong
one: `npm run build` creates it on any machine anyone has verified the
frontend on, and then `pytest` would behave differently there -- the catch-all
below would answer `/no-such-route` with `index.html` and
`tests/test_security_headers.py` would fail for a reason having nothing to do
with security headers. A directory only the image has makes the composition
deterministic everywhere else.

So `add_spa` is a no-op when the bundle is absent, which is every local run,
every test and every `run-vector-local.ps1` session -- there the Vite dev
server owns the page and proxies `/graphql` here.
"""

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from starlette.responses import FileResponse, Response
from starlette.staticfiles import StaticFiles


# Repository root, then `static/`. The same `parents[1]` shape
# `scripts/apply_migration.py` resolves `migrations/` with, so both agree
# about where the root is whether the code is running from a checkout or
# from /srv/vector in the image.
BUNDLE_DIR = Path(__file__).resolve().parents[1] / "static"

# Vite hashes every filename under /assets, so a given URL's bytes never
# change and the browser may keep it as long as it likes. `immutable` is the
# half that stops a conditional request on every reload.
ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"

# index.html is the one unhashed file and the one that names every hashed
# one. Cached even briefly, a browser keeps asking for the previous
# deployment's asset URLs after a release -- which 404 as soon as the old
# files are gone. Same value `frontend/nginx.conf` sets, for the same reason.
INDEX_CACHE_CONTROL = "no-store"

# What the APPLICATION's page is allowed to load, as distinct from what a
# JSON response is.
#
# `app/http_headers.py` picks its policy off the response's content type, and
# every `text/html` response until now was GraphiQL -- so index.html would
# otherwise be served under GRAPHIQL_CONTENT_SECURITY_POLICY, which permits
# scripts from unpkg.com that this page has no use for and forbids the Google
# Fonts stylesheet that `frontend/index.html` actually links. That middleware
# sets its headers with `setdefault` precisely so a route can decide for
# itself; this is the route that does.
#
# Each allowance is one the built page demonstrably needs:
#
#   script-src 'self'          the hashed module bundle, and nothing inline
#   style-src  + unsafe-inline React sets `style` attributes (the avatar hue
#                              among them), which `style-src` governs
#   fonts.googleapis/gstatic   the two <link>s in frontend/index.html
#   img-src https:             avatar URLs come from user records and are
#                              whatever host the account's picture is on
#   connect-src 'self'         `graphqlUrl` is the relative path `/graphql`;
#                              this is the line that would have to change if
#                              the API were ever moved off this origin, which
#                              is a useful thing for it to cost
#   frame-ancestors 'none'     no framing, as for the API
#   base-uri 'none'            an injected <base> would repoint every hashed
#                              asset URL at another host
#   form-action 'self'         nothing here posts a form anywhere else
SPA_CONTENT_SECURITY_POLICY = (
    "default-src 'none'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' data: https://fonts.gstatic.com; "
    "img-src 'self' data: https:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'self'"
)


class _HashedAssets(StaticFiles):
    """StaticFiles that says how long its files may be kept.

    Starlette sets `etag` and `last-modified` and no `Cache-Control` at all,
    which means a revalidation round trip per asset per page load. Subclassed
    rather than layered as middleware because the answer is a property of
    this one mount -- these filenames carry a content hash and nothing else
    served here does.
    """

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = ASSET_CACHE_CONTROL

        return response


def add_spa(app: FastAPI, directory: Path = BUNDLE_DIR) -> None:
    """Serve the built bundle, if this image has one.

    MUST BE CALLED LAST, after every router is included, and that is the
    whole of the ordering rule. Starlette matches routes in registration
    order and stops at the first hit, so the catch-all below cannot shadow
    `/graphql`, `/healthz`, `/readyz` or either `/integrations` mount --
    they are already in the table when it is added. Registered first it
    would shadow all of them, and the symptom would be `index.html` returned
    with HTTP 200 and `content-type: text/html` to every GraphQL operation:
    an Apollo parse error, and nothing anywhere saying "route".

    Absent bundle, nothing mounted. That is the normal case outside the
    image; see this module's docstring.
    """
    index = directory / "index.html"

    if not index.is_file():
        return

    assets = directory / "assets"

    if assets.is_dir():
        app.mount("/assets", _HashedAssets(directory=assets), name="assets")

    # The SPA fallback, and the reason a deep link survives a refresh.
    # React Router owns `/login`, `/onboarding`, `/<workspace>/issues/<id>`
    # and the rest (see frontend/src/app/routes/paths.ts); the server has no
    # file for any of them, and answering 404 would break every URL the
    # product hands out the moment somebody reloads the page on one.
    #
    # GET (and, via Starlette, HEAD) only. A POST to an unrouted path still
    # gets its 404 rather than a page, which is both correct for an API
    # client and what `tests/test_request_body_limit.py` asserts.
    @app.get("/{spa_path:path}", include_in_schema=False)
    async def _serve_index(spa_path: str) -> FileResponse:
        return FileResponse(
            index,
            headers={
                "Cache-Control": INDEX_CACHE_CONTROL,
                "Content-Security-Policy": SPA_CONTENT_SECURITY_POLICY,
            },
        )
