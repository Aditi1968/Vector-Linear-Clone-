"""The bundle is served without the API being served over.

`add_spa` registers a `/{path:path}` catch-all, which is the broadest route
this application has and the only one whose correctness is entirely a
question of WHEN it was registered. Registered before the routers it would
answer /graphql with the HTML page -- HTTP 200, `content-type: text/html`,
and an Apollo parse error naming nothing -- and every test in the suite that
exercises GraphQL would still pass, because they all reach the resolvers
directly rather than over the mount.

So the assertions here are deliberately about the composed application from
`create_app`, over HTTP, with a real bundle on disk. A stand-in FastAPI with
`add_spa` called on it would test the function and not the composition, and
the ordering is the composition.
"""

import httpx
import pytest

from app.config import get_settings
from app.http_limits import MAX_REQUEST_BODY_BYTES
from app.main import create_app
from app.spa import (
    ASSET_CACHE_CONTROL,
    INDEX_CACHE_CONTROL,
    SPA_CONTENT_SECURITY_POLICY,
    add_spa,
)

from tests.test_app_composition import build_client
from tests.test_settings import PLACEHOLDER_DSN, use_environment


INDEX_MARKER = '<div id="root"></div>'
ASSET_MARKER = "console.log('vector')"


@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_settings.cache_clear()

    yield

    get_settings.cache_clear()


@pytest.fixture
def bundle(tmp_path):
    """A directory shaped like `vite build`'s output, and nothing more.

    Two files, because two is what the real bundle has: one unhashed
    index.html and one hashed asset. The names below are the only thing the
    module cares about.
    """
    directory = tmp_path / "bundle"
    assets = directory / "assets"
    assets.mkdir(parents=True)

    (directory / "index.html").write_text(
        f"<!doctype html><html><body>{INDEX_MARKER}</body></html>",
        encoding="utf-8",
    )
    (assets / "index-abc123.js").write_text(ASSET_MARKER, encoding="utf-8")

    return directory


def build_app(monkeypatch, tmp_path, bundle=None):
    """The composed application, with the bundle mounted after the routers.

    `add_spa` is called a second time here rather than the module constant
    being patched, and the two calls do not conflict: the one inside
    `create_app` finds no `static/` directory in the test environment and
    returns having registered nothing at all. This one therefore lands in
    exactly the position the real one occupies -- last.
    """
    use_environment(
        monkeypatch,
        tmp_path,
        DATABASE_URL=PLACEHOLDER_DSN,
        ENVIRONMENT="production",
    )

    application = create_app()

    if bundle is not None:
        add_spa(application, bundle)

    return application


async def test_a_deep_link_survives_a_direct_get(monkeypatch, tmp_path, bundle):
    """The whole reason the catch-all exists.

    React Router owns `/<workspace>/projects/<id>` and the server has no file
    for it. Answering 404 would break every URL the product hands out the
    moment somebody reloads the page on one.
    """
    async with build_client(build_app(monkeypatch, tmp_path, bundle)) as client:
        response = await client.get("/acme/projects/6f3d9c1e-0000-7000-8000-0000")

    assert response.status_code == 200
    assert INDEX_MARKER in response.text


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/login",
        "/register",
        "/onboarding",
        "/acme/issues",
        "/acme/semantic-search",
    ],
)
async def test_every_shape_of_route_the_router_declares_is_answered(
    monkeypatch, tmp_path, bundle, path
):
    """Public, unscoped and workspace-scoped, as `paths.ts` spells them."""
    async with build_client(build_app(monkeypatch, tmp_path, bundle)) as client:
        response = await client.get(path)

    assert response.status_code == 200, path
    assert INDEX_MARKER in response.text


async def test_the_api_is_not_served_over(monkeypatch, tmp_path, bundle):
    """The ordering assertion, and the reason this file exists.

    /graphql must still be GraphQL and /healthz must still be JSON, with the
    catch-all in the table. Both are checked by CONTENT rather than by status,
    because the failure this guards against answers 200 -- with the wrong body.
    """
    async with build_client(build_app(monkeypatch, tmp_path, bundle)) as client:
        health = await client.get("/healthz")
        graphql = await client.post("/graphql", json={"query": "{ __typename }"})

    assert health.json() == {"status": "ok"}
    assert graphql.json()["data"] == {"__typename": "Query"}


async def test_an_absent_bundle_mounts_nothing(monkeypatch, tmp_path):
    """No bundle, no catch-all -- which is every local run and every test.

    Without this the module would have to be reasoned about rather than
    checked, and `tests/test_security_headers.py`'s 404 assertions depend on
    the answer.
    """
    async with build_client(build_app(monkeypatch, tmp_path)) as client:
        response = await client.get("/no-such-route")

    assert response.status_code == 404


async def test_the_index_is_never_cached_and_the_hashed_asset_always_is(
    monkeypatch, tmp_path, bundle
):
    """The two halves of the release problem.

    index.html names every hashed file, so a browser that cached it keeps
    asking for the previous deployment's asset URLs -- which 404 as soon as
    those files are gone. The hashed files are the opposite case: the URL
    cannot change content, so revalidating one is a round trip for nothing.
    """
    async with build_client(build_app(monkeypatch, tmp_path, bundle)) as client:
        index = await client.get("/")
        asset = await client.get("/assets/index-abc123.js")

    assert index.headers["cache-control"] == INDEX_CACHE_CONTROL
    assert asset.status_code == 200
    assert asset.text == ASSET_MARKER
    assert asset.headers["cache-control"] == ASSET_CACHE_CONTROL


async def test_the_page_gets_its_own_policy_rather_than_graphiqls(
    monkeypatch, tmp_path, bundle
):
    """`app/http_headers.py` picks a policy off the content type.

    Every `text/html` response before this one was GraphiQL, so index.html
    would inherit a policy that permits unpkg.com and forbids the Google
    Fonts stylesheet `frontend/index.html` actually links. The middleware
    uses `setdefault` precisely so a route can decide for itself; this
    asserts that the route does, and that the decision survives composition.
    """
    async with build_client(build_app(monkeypatch, tmp_path, bundle)) as client:
        response = await client.get("/login")

    policy = response.headers["content-security-policy"]

    assert policy == SPA_CONTENT_SECURITY_POLICY
    assert "unpkg.com" not in policy
    assert "https://fonts.googleapis.com" in policy
    assert "frame-ancestors 'none'" in policy


async def test_an_oversized_body_is_still_refused_before_routing(
    monkeypatch, tmp_path, bundle
):
    """The catch-all must not become a way past the request body limit.

    `add_request_body_limit` wraps the application rather than the GraphQL
    mount, so it applies before routing decides anything -- including before
    the broadest route in the table gets a look.
    """
    async with build_client(build_app(monkeypatch, tmp_path, bundle)) as client:
        response = await client.post(
            "/acme/issues",
            content=b"x" * (MAX_REQUEST_BODY_BYTES + 1024),
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 413


async def test_the_bundle_directory_cannot_be_escaped(monkeypatch, tmp_path, bundle):
    """A traversal out of /assets must not read the filesystem.

    Starlette's StaticFiles is what refuses this and the subclass changes
    only a response header, but "we did not break the thing that was safe"
    is worth one assertion rather than an assumption.
    """
    outside = tmp_path / "secret.txt"
    outside.write_text("not for you", encoding="utf-8")

    transport = httpx.ASGITransport(app=build_app(monkeypatch, tmp_path, bundle))

    async with httpx.AsyncClient(
        transport=transport, base_url="http://vector.test"
    ) as client:
        # Sent raw so httpx does not normalise the traversal away before it
        # reaches the application.
        response = await client.request(
            "GET", httpx.URL("http://vector.test/assets/../../secret.txt")
        )

    assert "not for you" not in response.text
