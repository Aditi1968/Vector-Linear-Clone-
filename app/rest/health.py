from fastapi import APIRouter, HTTPException, status

from app.db import get_pool


router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness: the API process is running. Never touches PostgreSQL."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz() -> dict[str, str]:
    """Readiness: the API process can actually reach PostgreSQL."""
    try:
        pool = get_pool()

        async with pool.acquire() as connection:
            await connection.fetchval("SELECT 1")
    except Exception:
        # `from None` keeps the driver/DSN details out of the response.
        # The cause will be logged once structlog lands.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service unavailable",
        ) from None

    return {"status": "ready"}
