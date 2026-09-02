"""The composition root fails fast, or not at all. No database involved.

create_app is the only place that resolves settings eagerly, which makes it
the only place a misconfigured deployment can be caught before it starts
serving. These tests pin both halves of that: that it refuses to build an
application it cannot configure, and that the environment it was given
actually reaches the parts whose behaviour depends on it.
"""

import httpx
import pytest
from pydantic import ValidationError

from app.config import Environment, get_settings
from app.graphql.context import VectorContext, get_context
from app.main import create_app

from tests.test_settings import (
    ALL_ENVIRONMENTS,
    PLACEHOLDER_DSN,
    missing_fields,
    use_environment,
)


@pytest.fixture(autouse=True)
def clear_settings_cache():
    """Never let a test's synthetic environment outlive it."""
    get_settings.cache_clear()

    yield

    get_settings.cache_clear()


def build_client(application) -> httpx.AsyncClient:
    """An HTTP client speaking to the application in-process.

    ASGITransport does not run the lifespan, so the pool is never created
    and nothing here reaches PostgreSQL. The GraphQL context is overridden
    for the same reason: the real one borrows a pool that does not exist.
    """
    application.dependency_overrides[get_context] = lambda: VectorContext(
        issue_service=None
    )

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://vector.test",
    )


def test_a_missing_environment_fails_when_the_application_is_created(
    monkeypatch, tmp_path
):
    """The failure has to land here, where a deployment still notices it.

    Anywhere later -- first request, first production query -- and the
    process is already up and answering, which is exactly the state a
    fail-closed setting exists to prevent.
    """
    use_environment(monkeypatch, tmp_path, DATABASE_URL=PLACEHOLDER_DSN)

    with pytest.raises(ValidationError) as raised:
        create_app()

    assert missing_fields(raised.value) == {"environment"}


@pytest.mark.parametrize("environment", ALL_ENVIRONMENTS)
async def test_an_explicit_environment_produces_a_working_application(
    monkeypatch, tmp_path, environment: Environment
):
    use_environment(
        monkeypatch,
        tmp_path,
        DATABASE_URL=PLACEHOLDER_DSN,
        ENVIRONMENT=environment,
    )

    async with build_client(create_app()) as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_a_composed_production_application_serves_no_graphiql(
    monkeypatch, tmp_path
):
    """The environment must survive the trip from settings to the mount.

    build_graphql_router is unit-tested with an environment handed to it
    directly, which cannot catch a composition root that reads the setting
    and then passes the wrong thing.
    """
    use_environment(
        monkeypatch,
        tmp_path,
        DATABASE_URL=PLACEHOLDER_DSN,
        ENVIRONMENT="production",
    )

    async with build_client(create_app()) as client:
        response = await client.get("/graphql", headers={"accept": "text/html"})

    # 404, specifically: production routes the IDE nowhere. A test for
    # merely "not 200" would be satisfied by a 500 from a broken mount,
    # and report this protection as covered when it had not been exercised.
    assert response.status_code == 404
