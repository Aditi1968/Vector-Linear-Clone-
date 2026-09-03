"""Production hardening of the GraphQL surface. No database involved.

`build_schema` and `build_graphql_router` carry the two switches that decide
what an anonymous caller can see in production: whether the schema answers
introspection, and whether GraphiQL is served. Both invert silently if they
regress -- nothing crashes, the app just starts handing out its schema -- so
each environment is asserted explicitly rather than inferred from the others.
"""

import inspect

import pytest
from pydantic import ValidationError as SettingsValidationError

import app.db
from app.config import Environment, get_settings
from app.domain.pagination import IssuePage
from app.graphql.context import get_context
from app.graphql.router import build_graphql_router
from app.graphql.schema import build_schema

from tests.conftest import make_entity


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
  issues(first: 1) {
    nodes {
      id
      title
    }
  }
}
"""


class Context:
    def __init__(self, issue_service):
        self.issue_service = issue_service


class FakeIssueService:
    """Returns a fixed page without any database access."""

    def __init__(self, page: IssuePage):
        self._page = page

    async def list(self, *, first: int, after: str | None):
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
