from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.db import connect, disconnect
from app.graphql.router import build_graphql_router
from app.graphql.schema import build_schema
from app.http_limits import add_request_body_limit
from app.rest.health import router as health_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await connect()

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

    return app
