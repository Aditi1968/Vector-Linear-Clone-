import asyncio

from fastapi import APIRouter, HTTPException, status

from app.db import get_pool


router = APIRouter(tags=["health"])

# Budget for the whole readiness check, deliberately a fraction of the query
# timeout in app.db. A probe answers a scheduler that has to act on the
# answer, so it must give up long before the 10s a real query is allowed to
# take: a probe that hangs is worse than one that fails, because an
# orchestrator can route traffic away from an unready instance but can only
# wait on a silent one.
READINESS_TIMEOUT_SECONDS = 2.0

# What one acquire may take -- and, because asyncpg records the value from
# the acquire call as the budget for giving the same connection back
# (pool.py:888-890, then 931-937), what the release may take as well.
#
# Passing it at all is the part that matters. pool.acquire() with no timeout
# records None, and None makes the release path explicitly unbounded: it
# waits on any in-flight cancellation and then on RESET ALL, both against a
# server that has stopped answering, inside an asyncio.shield that no
# further cancellation can reach (pool.py:221-239). That is how a probe ends
# up holding a pooled connection with nothing left that can free it.
#
# Half the probe's budget rather than all of it, because asyncpg charges it
# twice -- once on the way in, once on the way out. So a probe abandoned at
# READINESS_TIMEOUT_SECONDS has retired, and given its connection back, one
# ACQUIRE_TIMEOUT_SECONDS after that. At the full budget the tail would be a
# second whole budget instead.
ACQUIRE_TIMEOUT_SECONDS = READINESS_TIMEOUT_SECONDS / 2

# The readiness check currently in flight, if any. Module state because a
# probe outlives the request that started it: the request walks away at its
# budget, the probe keeps its pooled connection until the driver or the OS
# lets go, and the next request has to join that one rather than start
# another.
_probe: asyncio.Task | None = None


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness: the API process is running. Never touches PostgreSQL."""
    return {"status": "ok"}


async def _reach_database() -> None:
    pool = get_pool()

    async with pool.acquire(timeout=ACQUIRE_TIMEOUT_SECONDS) as connection:
        await connection.fetchval("SELECT 1")


def _forget_probe(task: asyncio.Task) -> None:
    """Clear the finished probe and consume whatever it ended with.

    Reading the exception is not housekeeping: an abandoned probe usually
    ends in one, and an exception nobody reads is reported against the event
    loop when the task is collected.
    """
    global _probe

    if _probe is task:
        _probe = None

    if not task.cancelled():
        task.exception()


def _current_probe() -> asyncio.Task:
    """The probe in flight, started if there is not already one.

    Single-flight, because every probe holds one of the pool's five
    connections until it unwinds and an orchestrator re-probes every few
    seconds. A fresh probe per request during an outage would exhaust the
    pool through the health check itself, and readiness would then stay
    broken after the database came back, having run out of connections to
    prove it with.
    """
    global _probe

    if _probe is None or _probe.done():
        _probe = asyncio.create_task(_reach_database())
        _probe.add_done_callback(_forget_probe)

    return _probe


def _unavailable() -> HTTPException:
    """A 503 that says nothing about the driver, the DSN or the failure.

    /readyz is reachable by anyone who can reach the service. The cause will
    be logged once structlog lands.
    """
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Service unavailable",
    )


@router.get("/readyz")
async def readyz() -> dict[str, str]:
    """Readiness: the API process can actually reach PostgreSQL.

    Two bounds, doing two different jobs, and neither covers the other.

    This one bounds the *answer*. The check runs as its own task and is
    waited on rather than awaited, so the handler can walk away without
    first waiting to hear that the probe stopped. That distinction is the
    whole fix: cancelling asyncpg mid-query is itself a round trip to the
    server that has stopped answering (connection.py:1652), so anything
    bounding an await *by* cancelling it -- asyncio.timeout included --
    waits forever for a cancellation that cannot land. asyncio.wait bounds
    the waiting instead, which nothing across the socket can hold up.

    ACQUIRE_TIMEOUT_SECONDS bounds the *probe*, so that the connection it
    holds comes back rather than being stranded. Answering promptly while
    leaking a connection per outage would trade a hung probe for a drained
    pool.
    """
    probe = _current_probe()

    done, _ = await asyncio.wait({probe}, timeout=READINESS_TIMEOUT_SECONDS)

    if not done:
        # Requested and deliberately not awaited, for the reason above. A
        # probe that ignores it stays in flight, and the next request joins
        # it rather than opening the outage a second connection.
        probe.cancel()

        raise _unavailable()

    if probe.cancelled() or probe.exception() is not None:
        raise _unavailable()

    return {"status": "ready"}
