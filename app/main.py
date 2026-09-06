from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.db import connect, disconnect
from app.graphql.router import build_graphql_router
from app.graphql.schema import build_schema
from app.http_limits import add_request_body_limit
from app.rest.github import router as github_router
from app.rest.health import router as health_router
from app.rest.slack import router as slack_router
from app.services.passwords import warm_password_hashing


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await connect()

    # Builds the decoy hash the log-in path verifies against when no account
    # matches the submitted address. It costs one argon2 hash, and paying for
    # it here is the point: otherwise the first unknown-address log-in of the
    # process pays for two hashes where a wrong-password log-in pays for one,
    # which is exactly the timing difference the decoy exists to erase.
    await warm_password_hashing()

    try:
        yield
    finally:
        await disconnect()


def create_app() -> FastAPI:
    """Compose the application.

    This is the composition root, and the one place that resolves settings
    eagerly: a deployment missing DATABASE_URL or ENVIRONMENT should fail
    here, at startup, rather than on the first request. Everything below
    takes what it needs as an argument.

    Deliberately not called at import time. A module-level `app =
    create_app()` made importing this module resolve settings and build an
    application as a side effect, so a test or a linter that merely
    imported it failed for want of a runtime environment it never used --
    and made the composition root itself unimportable, and so untestable,
    on any machine it was supposed to fail on. Servers name the factory:
    `uvicorn app.main:create_app --factory`.
    """
    settings = get_settings()

    app = FastAPI(
        title="Vector",
        lifespan=lifespan,
    )

    add_request_body_limit(app)

    app.include_router(
        build_graphql_router(
            build_schema(settings.environment),
            settings.environment,
        ),
        prefix="/graphql",
    )
    app.include_router(health_router)

    # Mounted unconditionally, including on a deployment with no GitHub App.
    # Gating the mount on configuration would make an unconfigured deployment
    # answer 404 from the router and a configured one 404 from the handler,
    # which are the same answer arrived at two ways -- and the second is the
    # one that stays right when the settings change without a redeploy.
    app.include_router(github_router)
    # Mounted unconditionally, including on a deployment with no Slack
    # credentials. The routes exist and answer 503 "not configured" rather
    # than 404, because a mount that depended on configuration would make a
    # misconfigured deployment indistinguishable from one where the code was
    # never deployed -- and would mean the routing table differs between
    # environments, which is the thing that makes a staging test meaningless.
    app.include_router(slack_router)

    return app
