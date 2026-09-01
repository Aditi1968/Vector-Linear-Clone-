from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.db import connect, disconnect
from app.graphql.router import graphql_router
from app.rest.health import router as health_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    await connect()

    try:
        yield
    finally:
        await disconnect()


app = FastAPI(
    title="Vector",
    lifespan=lifespan,
)

app.include_router(graphql_router, prefix="/graphql")
app.include_router(health_router)
