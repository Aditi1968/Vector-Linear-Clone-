import asyncpg

from app.config import get_settings


_pool: asyncpg.Pool | None = None


async def connect() -> None:
    global _pool

    # Settings are resolved here rather than at import so that importing
    # this module never requires a configured database.
    settings = get_settings()

    _pool = await asyncpg.create_pool(
        dsn=settings.database_url.get_secret_value(),
        min_size=1,
        max_size=5,
    )


async def disconnect() -> None:
    global _pool

    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool has not been initialized")

    return _pool
