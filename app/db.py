import asyncpg

from app.config import get_settings


# How long the *client* waits for a result. Without it a caller waits on
# PostgreSQL indefinitely: a dropped connection or a blocked backend holds
# the request open rather than failing it, and the pool loses a connection
# for the duration.
COMMAND_TIMEOUT_SECONDS = 10.0

# How long *PostgreSQL* spends executing the statement, in milliseconds and
# as a string because asyncpg forwards server_settings verbatim.
#
# Kept strictly below COMMAND_TIMEOUT_SECONDS, and that ordering is the
# entire point of having both. Whichever bound fires first decides how the
# failure gets delivered. PostgreSQL ending its own statement reports the
# error back over the connection already open. The client giving up first
# makes asyncpg open a *second* connection to the same server to send a
# CancelRequest (connection.py:1652) -- the one round trip that cannot
# complete when the server is what became unreachable, and the one that
# leaves a pooled connection stuck in release.
#
# Equal values hand that fragile path every single timeout, because the
# client bound always wins a tie. The 500ms of headroom is what makes
# server-side termination the normal case and the cancel round trip the
# exception.
STATEMENT_TIMEOUT_MS = "9500"


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
        command_timeout=COMMAND_TIMEOUT_SECONDS,
        # Sent in the startup packet, which makes it the session default that
        # RESET ALL restores to. asyncpg runs RESET ALL when a connection goes
        # back to the pool, so an `init` coroutine issuing SET would hold for
        # one acquire and be stripped from every acquire after it.
        server_settings={"statement_timeout": STATEMENT_TIMEOUT_MS},
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
