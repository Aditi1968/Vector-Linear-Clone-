"""Production hardening of the GraphQL surface. No database involved.

`build_schema` and `build_graphql_router` carry the two switches that decide
what an anonymous caller can see in production: whether the schema answers
introspection, and whether GraphiQL is served. Both invert silently if they
regress -- nothing crashes, the app just starts handing out its schema -- so
each environment is asserted explicitly rather than inferred from the others.
"""

import inspect

import httpx
import pytest
from pydantic import ValidationError as SettingsValidationError

import app.db
from app.config import Environment, get_settings
from app.domain.pagination import IssuePage
from app.graphql.context import get_context
from app.graphql.router import build_graphql_router
from app.graphql.schema import build_schema
from app.main import create_app

from tests.conftest import graphql_context, make_entity
from tests.test_settings import PLACEHOLDER_DSN, use_environment


ALL_ENVIRONMENTS: list[Environment] = ["development", "test", "production"]
NON_PRODUCTION: list[Environment] = ["development", "test"]


INTROSPECTION_QUERY = """
query Introspect {
  __schema {
    queryType {
      name
    }
  }
}
"""

ISSUES_QUERY = """
query ListIssues {
  issues(workspaceSlug: "acme", first: 1) {
    nodes {
      id
      title
    }
  }
}
"""


def Context(issue_service):
    """The real context, wired to one fake service.

    `issues` names a workspace now, so the request has to carry an identity
    and a membership before it reaches the issue service; `graphql_context`
    supplies working fakes for both.
    """
    return graphql_context(issue_service=issue_service)


class FakeIssueService:
    """Returns a fixed page without any database access."""

    def __init__(self, page: IssuePage):
        self._page = page

    async def list(self, *, scope, first: int, after: str | None, **kwargs):
        return self._page


@pytest.fixture
def unconfigured(tmp_path, monkeypatch):
    """No DATABASE_URL in the environment and no .env within reach.

    The repository root holds a .env, and pydantic-settings resolves that
    path relative to the working directory, so the chdir is what actually
    makes configuration unavailable.
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()

    yield

    # Leave no settings object cached from a deliberately broken environment.
    get_settings.cache_clear()


async def test_production_refuses_introspection():
    """Production must not hand its schema to an anonymous caller."""
    result = await build_schema("production").execute(INTROSPECTION_QUERY)

    assert result.errors is not None
    assert result.data is None


@pytest.mark.parametrize("environment", NON_PRODUCTION)
async def test_introspection_is_available_outside_production(
    environment: Environment,
):
    """Tooling outside production depends on introspection answering."""
    result = await build_schema(environment).execute(INTROSPECTION_QUERY)

    assert result.errors is None
    assert result.data == {"__schema": {"queryType": {"name": "Query"}}}


@pytest.mark.parametrize("environment", ALL_ENVIRONMENTS)
async def test_ordinary_queries_execute_in_every_environment(
    environment: Environment,
):
    """Disabling introspection must not disable the API it protects."""
    entity = make_entity(1)
    page = IssuePage(nodes=[entity], has_next_page=False, end_cursor=None)

    result = await build_schema(environment).execute(
        ISSUES_QUERY,
        context_value=Context(FakeIssueService(page)),
    )

    assert result.errors is None
    assert result.data == {
        "issues": {"nodes": [{"id": str(entity.id), "title": entity.title}]}
    }


async def test_building_requires_no_configuration(unconfigured):
    """A schema and a router must build where no database is configured.

    Resolving settings is asserted to fail first, so that a stray
    DATABASE_URL cannot let this pass without proving anything.
    """
    with pytest.raises(SettingsValidationError):
        get_settings()

    schema = build_schema("production")
    router = build_graphql_router(schema, "production")

    assert router.graphql_ide is None

    result = await schema.execute(
        ISSUES_QUERY,
        context_value=Context(
            FakeIssueService(IssuePage(nodes=[], has_next_page=False, end_cursor=None))
        ),
    )

    assert result.errors is None


def test_production_does_not_serve_graphiql():
    """GraphiQL is a schema browser; production must not publish one."""
    router = build_graphql_router(build_schema("production"), "production")

    # strawberry 0.326.0 reads this attribute per request to decide whether
    # to render the IDE, so it is the setting itself and not a copy of it.
    assert router.graphql_ide is None


@pytest.mark.parametrize("environment", NON_PRODUCTION)
def test_graphiql_is_served_outside_production(environment: Environment):
    router = build_graphql_router(build_schema(environment), environment)

    assert router.graphql_ide == "graphiql"


@pytest.mark.parametrize("environment", ALL_ENVIRONMENTS)
def test_queries_via_get_are_refused_in_every_environment(
    environment: Environment,
):
    """Queries over GET are CSRF-reachable, so no environment allows them."""
    router = build_graphql_router(build_schema(environment), environment)

    assert router.allow_queries_via_get is False


async def test_router_is_built_without_a_pool():
    """Building the router must not reach for the database.

    The pool belongs to the FastAPI lifespan, which has not run here: the
    context factory is wired for later use, never called during assembly.
    """
    assert app.db._pool is None

    build_graphql_router(build_schema("test"), "test")

    assert app.db._pool is None

    # And the wired factory really would have needed a pool.
    with pytest.raises(RuntimeError):
        await get_context()


def test_router_wires_the_application_context_factory():
    router = build_graphql_router(build_schema("test"), "test")

    # strawberry wraps context_getter in a FastAPI dependency; the callable
    # handed in survives as the `custom_context` parameter's default.
    signature = inspect.signature(router.context_getter)
    parameter = signature.parameters["custom_context"]

    assert parameter.default.dependency is get_context


# --------------------------------------------------------------------------
# What a cross-site page can make this endpoint do
# --------------------------------------------------------------------------
#
# The session travels as a cookie (`app/http_cookies.py` says why: HttpOnly is
# a cookie-only attribute and is the difference between an XSS bug that reads
# the DOM and one that walks off with a session), and the price of a cookie is
# CSRF. Three things stand between a page on another origin and a mutation
# made with this browser's session, and all three are one edit from being
# gone:
#
#   * SameSite=Lax, which withholds the cookie from a cross-site POST. Pinned
#     by tests/test_auth_graphql.py, on the Set-Cookie header itself.
#   * no CORS headers, so a script on another origin cannot read the reply
#     even when it can send the request.
#   * the endpoint's content-type refusal. This is the one that matters when
#     the other two are bypassed, because the content types a cross-site
#     `<form>` or a no-preflight `fetch` can produce -- form-encoded, plain
#     text, multipart -- are exactly the ones CORS lets through unasked.
#
# The last two have no test until here, and both would be silently undone by
# an ordinary-looking change: a `CORSMiddleware(allow_origins=["*"],
# allow_credentials=True)` added for a second frontend, or
# `multipart_uploads_enabled=True` added for file uploads.


@pytest.fixture
async def client(monkeypatch, tmp_path):
    """The real composition root, over ASGI, with no database behind it.

    `create_app` rather than a hand-wired router, on the argument
    `tests/test_security_headers.py` makes: middleware or a flag dropped from
    the composition root should fail a test rather than pass one against a
    stand-in. ASGITransport never runs the lifespan, so there is no pool and
    the context factory is overridden -- none of the assertions below reaches
    a resolver anyway, and the two that execute a document select
    `__typename`, which is answered from the schema.
    """
    use_environment(
        monkeypatch,
        tmp_path,
        DATABASE_URL=PLACEHOLDER_DSN,
        ENVIRONMENT="production",
    )

    application = create_app()
    application.dependency_overrides[get_context] = lambda: graphql_context(
        environment="production",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://vector.test",
    ) as client:
        yield client


# Two of the three content types a browser will send cross-origin with NO
# preflight -- the CORS "simple request" set. `fetch` with no custom headers
# produces either. Multipart is the third and gets its own test below,
# because refusing it takes a differently-shaped request to prove.
SIMPLE_CONTENT_TYPES = (
    "application/x-www-form-urlencoded",
    "text/plain;charset=UTF-8",
)


@pytest.mark.parametrize("content_type", SIMPLE_CONTENT_TYPES)
async def test_a_cors_simple_content_type_is_refused(client, content_type: str):
    """Only application/json executes, and json is not a simple content type.

    Requiring it is what forces a browser to preflight, and a preflight this
    application answers with no CORS headers at all is a preflight that fails
    -- so the request never leaves the browser. That chain is the whole
    defence, and it starts here.
    """
    response = await client.post(
        "/graphql",
        content='{"query":"{ __typename }"}',
        headers={"content-type": content_type},
    )

    assert response.status_code == 400
    assert "__typename" not in response.text


async def test_a_spec_shaped_multipart_upload_is_refused(client):
    """The third simple content type, and the one a plain `<form>` can post.

    Shaped as the GraphQL multipart request specification says -- an
    `operations` part holding the document and a `map` part beside it --
    rather than as a JSON body wearing a multipart header. That distinction
    is the whole value of this test: a malformed multipart body is refused
    for being malformed and would go on being refused after somebody set
    `multipart_uploads_enabled=True` for a file-upload feature. This one is
    accepted the moment that flag flips, which is the regression worth
    catching, because multipart needs no preflight and so is the one shape
    CORS cannot refuse on the application's behalf.
    """
    response = await client.post(
        "/graphql",
        files={
            "operations": (None, '{"query":"{ __typename }"}'),
            "map": (None, "{}"),
        },
    )

    assert response.status_code == 400
    assert "__typename" not in response.text


async def test_the_json_content_type_really_does_execute(client):
    """The control. An endpoint that refused every content type would satisfy
    the tests above and serve nobody."""
    response = await client.post("/graphql", json={"query": "{ __typename }"})

    assert response.status_code == 200
    assert response.json() == {"data": {"__typename": "Query"}}


async def test_no_cors_header_is_offered_to_another_origin(client):
    """Nothing here opts into cross-origin credentialed requests.

    Asserted as an absence, which is the only form this can take: there is no
    CORS middleware to inspect, and the regression would be somebody adding
    one. A single `Access-Control-Allow-Origin` beside
    `Access-Control-Allow-Credentials: true` turns every read in this schema
    into something any page on the internet can perform with the visitor's
    session.
    """
    preflight = await client.request(
        "OPTIONS",
        "/graphql",
        headers={
            "origin": "https://evil.example",
            "access-control-request-method": "POST",
            "access-control-request-headers": "content-type",
        },
    )
    simple = await client.post(
        "/graphql",
        json={"query": "{ __typename }"},
        headers={"origin": "https://evil.example"},
    )

    for response in (preflight, simple):
        assert not [
            name
            for name in response.headers
            if name.lower().startswith("access-control-")
        ], dict(response.headers)

    # And the preflight is not merely header-free: there is no handler for it.
    assert preflight.status_code == 405


async def test_http_level_batching_is_refused(client):
    """One document per request, so the operation limits bound the request.

    `app/graphql/limits.py` measures each operation in a document separately,
    which is correct because GraphQL executes one of them. A batch is a JSON
    ARRAY of requests, and every element of it would execute -- so a batch of
    twenty documents, each just inside the complexity budget, is twenty times
    the budget in one round trip. strawberry refuses batches unless asked;
    this is the assertion that nobody asked.
    """
    response = await client.post(
        "/graphql",
        json=[{"query": "{ __typename }"}, {"query": "{ __typename }"}],
    )

    assert response.status_code == 400
    assert "__typename" not in response.text
