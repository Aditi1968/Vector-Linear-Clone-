from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.db import connect, disconnect
from app.graphql.router import build_graphql_router
from app.graphql.schema import build_schema
from app.rest.health import router as health_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    await connect()

    try:
        yield
    finally:
        await disconnect()


def create_app() -> FastAPI:
    """Compose the application.

    This is the composition root, and the one place that resolves settings
    eagerly: a deployment missing DATABASE_URL should fail here, at startup,
    rather than on the first request. Everything below takes what it needs
    as an argument.
    """
    settings = get_settings()

    app = FastAPI(
        title="Vector",
        lifespan=lifespan,
    )

    app.include_router(
        build_graphql_router(
            build_schema(settings.environment),
            settings.environment,
        ),
        prefix="/graphql",
    )
    app.include_router(health_router)

    return app


app = create_app()
