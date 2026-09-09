"""Every response carries the hardening headers, including the failures.

There were none of these headers before `app/http_headers.py`. Neither the
application nor `frontend/nginx.conf` set one -- found by asking a running
stack for `/healthz` and reading what came back -- so this file exists to
keep that from being true again quietly.

The application under test is the real one from `create_app`, on the same
argument `test_request_body_limit.py` makes: middleware dropped from the
composition root should fail a test rather than pass one against a
hand-wired stand-in.
"""

import httpx
import pytest

from app.graphql.context import get_context
from app.http_headers import (
    API_CONTENT_SECURITY_POLICY,
    GRAPHIQL_CONTENT_SECURITY_POLICY,
    PERMISSIONS_POLICY,
    REFERRER_POLICY,
)
from app.main import create_app

from tests.conftest import graphql_context
from tests.test_settings import PLACEHOLDER_DSN, use_environment


EXPECTED = {
    "content-security-policy": API_CONTENT_SECURITY_POLICY,
    "x-content-type-options": "nosniff",
    "referrer-policy": REFERRER_POLICY,
    "permissions-policy": PERMISSIONS_POLICY,
}


@pytest.fixture
async def client(monkeypatch, tmp_path):
    use_environment(
        monkeypatch,
        tmp_path,
        DATABASE_URL=PLACEHOLDER_DSN,
        ENVIRONMENT="test",
    )

    # ASGITransport never runs the lifespan, so no pool exists. The two
    # tests that reach /graphql need a context the real factory would have
    # borrowed one for; the header assertions themselves care about none of
    # it, which is why the override is this bare.
    application = create_app()
    application.dependency_overrides[get_context] = lambda: graphql_context(
        environment="test",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://vector.test",
    ) as client:
        yield client


async def test_a_successful_response_carries_every_header(client):
    response = await client.get("/healthz")

    assert response.status_code == 200

    for header, value in EXPECTED.items():
        assert response.headers.get(header) == value, header


async def test_a_404_carries_them_too(client):
    """The reason this is middleware rather than a dependency.

    A route that does not exist is served by Starlette itself, so a
    dependency-based implementation would return this response bare -- and
    an error page is not less worth protecting than a successful one.
    """
    response = await client.get("/no-such-route")

    assert response.status_code == 404

    for header, value in EXPECTED.items():
        assert response.headers.get(header) == value, header


async def test_the_api_policy_refuses_framing_and_every_subresource(client):
    """`frame-ancestors 'none'` is the half that does real work here.

    It is also why X-Frame-Options is deliberately absent: a browser that
    reads the CSP ignores the older header, and setting both invites a
    reader to believe the protection comes from the one that does nothing.
    """
    response = await client.get("/healthz")
    policy = response.headers["content-security-policy"]

    assert "frame-ancestors 'none'" in policy
    assert "default-src 'none'" in policy
    assert "x-frame-options" not in {name.lower() for name in response.headers}


async def test_hsts_is_absent_over_plain_http(client):
    """Sent unconditionally it would be useless here and harmful locally.

    A browser ignores HSTS on a plain-HTTP response, and a developer who
    once loaded http://localhost would be pinned to an HTTPS listener that
    does not exist there until the max-age expired.
    """
    response = await client.get("/healthz")

    assert "strict-transport-security" not in {
        name.lower() for name in response.headers
    }


async def test_hsts_is_present_over_https(client):
    """The same request over TLS, which is the only place it means anything."""
    response = await client.get("https://vector.test/healthz")

    assert response.headers.get("strict-transport-security") == "max-age=31536000"
    # Neither is a commitment this application can make about names it does
    # not own, and neither is reversible inside the max-age.
    assert "includeSubDomains" not in response.headers["strict-transport-security"]
    assert "preload" not in response.headers["strict-transport-security"]


async def test_graphiql_gets_a_policy_that_lets_it_render(client):
    """A JSON policy applied to the HTML tool would serve a blank page.

    `test` is not `production`, so the route serves GraphiQL; the middleware
    picks the looser policy off the response's own content type rather than
    re-deciding that rule from the path or the settings.
    """
    response = await client.get("/graphql", headers={"accept": "text/html"})

    assert response.headers["content-type"].startswith("text/html")
    assert (
        response.headers["content-security-policy"] == GRAPHIQL_CONTENT_SECURITY_POLICY
    )
    # Still unframeable, which is the one part that must not relax for a
    # developer tool that talks to a live database.
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


async def test_a_graphql_response_gets_the_json_policy(client):
    """The same route, answering JSON, must not inherit GraphiQL's leniency."""
    response = await client.post(
        "/graphql",
        json={"query": "{ __typename }"},
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 200
    assert response.headers["content-security-policy"] == API_CONTENT_SECURITY_POLICY
    assert "unsafe-inline" not in response.headers["content-security-policy"]
