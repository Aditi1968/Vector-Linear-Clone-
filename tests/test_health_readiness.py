"""The health endpoints under the conditions an orchestrator creates.

/healthz answers "is this process alive"; /readyz answers "can it serve
traffic". Conflating them restarts a healthy process because a database
blipped, so liveness is asserted to hold with no pool at all.

Readiness has the opposite failure: a probe that hangs is worse than one
that fails, because a scheduler can route around an unready instance but can
only wait on a silent one. Two distinct things can hold /readyz open, and
they need different fakes:

* No connection to be had. pool.acquire() has no bound of its own, so it
  waits for one forever. That wait answers cancellation.
* A connection whose query never returns *and never unwinds*. asyncpg
  cancels a running query by opening a second connection to the same server
  to send the CancelRequest, so when the server is the unreachable thing,
  cancelling hangs where the query hung. That wait does not answer
  cancellation, and anything that bounds an await *by* cancelling it -- like
  asyncio.timeout -- cannot fire.

app.main is deliberately not imported. create_app() resolves settings and
assembles the GraphQL surface, none of which a health probe involves, and
coupling these tests to that assembly would make them fail for reasons that
have nothing to do with health.
"""

import asyncio
import contextlib
import time
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

import app.db
import app.rest.health as health
from app.db import COMMAND_TIMEOUT_SECONDS
from app.rest.health import (
    ACQUIRE_TIMEOUT_SECONDS,
    READINESS_TIMEOUT_SECONDS,
    router,
)


# Scheduling and ASGI overhead on top of the probe's own budget. The timing
# assertion is on the total, so removing the bound fails the test instead of
# passing whenever the request eventually completes.
PROMPTNESS_SLACK_SECONDS = 1.0

# Far above any bounded answer, so it only ever trips on an unbounded one.
# Without it, an unbounded /readyz would hang the suite rather than report a
# failure.
HANG_GUARD_SECONDS = 15.0

# Loop iterations for an abandoned probe to unwind once its stall is ended.
# Not a synchronisation point for any assertion -- purely so that no task is
# still pending when the event loop closes.
RETIREMENT_SECONDS = 0.05

# Stands in for the sort of detail a driver puts in its exception messages.
LEAKY_DETAIL = "postgresql://vector:PLACEHOLDER@db.internal:5432/vector"


class _Acquired:
    def __init__(self, connection):
        self._connection = connection

    async def __aenter__(self):
        return self._connection

    async def __aexit__(self, *exc_info):
        return False


class ProbePool:
    """Hands out one connection, recording how and how often it was asked.

    The count is the evidence for how many probes are in flight: each one
    holds a connection for as long as it lives. The recorded timeouts are
    the evidence that the probe asked for a *bounded* one.
    """

    def __init__(self, connection):
        self.connection = connection
        self.acquire_count = 0
        self.acquire_timeouts: list[float | None] = []

    def acquire(self, *, timeout=None):
        self.acquire_count += 1
        self.acquire_timeouts.append(timeout)

        return _Acquired(self.connection)


class ReadyConnection:
    """Answers the readiness query and records what was asked."""

    def __init__(self):
        self.queries: list[str] = []

    async def fetchval(self, query, *args):
        self.queries.append(query)

        return 1


class UnresponsiveConnection:
    """A query that never returns and does not unwind when cancelled.

    This is what an unreachable server looks like from inside asyncpg. The
    driver's cancel path (connection.py:1652) opens a *new* connection to
    the same server to send the CancelRequest and resolves the waiter only
    in its own finally, so a CancelledError aimed at the query cannot be
    delivered until a connection to an unreachable server succeeds. The
    swallowed cancellation below is that, modelled: the caller asks, and
    nothing happens.

    `recover()` ends the stall so a test can retire the probe it abandoned.
    A deployment gets the same effect when the OS finally gives up on the
    socket.
    """

    def __init__(self):
        self._recovered = asyncio.Event()
        self.cancellations = 0

    def recover(self) -> None:
        self._recovered.set()

    async def fetchval(self, query, *args):
        while not self._recovered.is_set():
            try:
                await self._recovered.wait()
            except asyncio.CancelledError:
                self.cancellations += 1

        raise ConnectionError("server was unreachable")


class _NeverAcquired:
    async def __aenter__(self):
        await asyncio.Event().wait()

    async def __aexit__(self, *exc_info):
        return False


class ExhaustedPool:
    """Every connection checked out and none coming back.

    acquire() is not slow here, it simply never returns -- which is what
    exhaustion looks like from the endpoint's side. The timeout is accepted
    and then ignored on purpose: the handler's own bound has to hold even
    where the pool does not honour the one it was given.
    """

    def __init__(self):
        self.acquire_count = 0

    def acquire(self, *, timeout=None):
        self.acquire_count += 1

        return _NeverAcquired()


class _StrandedRelease:
    """An acquire whose *release* nothing can interrupt.

    asyncpg hands a connection back inside asyncio.shield (pool.py:937), on
    a budget it took from the acquire call. Given no budget, that release
    waits without bound -- first on any in-flight cancellation, then on
    RESET ALL (pool.py:221-239) -- against a server that has stopped
    answering. Both halves of why nothing ends it are modelled here: the
    shield, so the inner wait survives cancellation, and the swallowing, so
    the outer one is not interruptible either.

    The query succeeds, which is deliberate. It leaves the release as the
    only thing that can hold the handler.
    """

    def __init__(self, connection, released: asyncio.Event):
        self._connection = connection
        self._released = released
        self.cancellations = 0

    async def __aenter__(self):
        return self._connection

    async def __aexit__(self, *exc_info):
        while not self._released.is_set():
            try:
                await asyncio.shield(self._released.wait())
            except asyncio.CancelledError:
                self.cancellations += 1

        return False


class StrandedReleasePool:
    """A pool that answers queries and then will not take the connection back."""

    def __init__(self, connection):
        self.connection = connection
        self.released = asyncio.Event()
        self.acquisitions: list[_StrandedRelease] = []

    def acquire(self, *, timeout=None):
        acquired = _StrandedRelease(self.connection, self.released)
        self.acquisitions.append(acquired)

        return acquired

    def recover(self) -> None:
        self.released.set()


@pytest.fixture
def health_app() -> FastAPI:
    """An application carrying the health router and nothing else."""
    application = FastAPI()
    application.include_router(router)

    return application


@pytest.fixture
async def client(health_app):
    transport = httpx.ASGITransport(app=health_app)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://health.test"
    ) as client:
        yield client


@pytest.fixture(autouse=True)
def no_inherited_probe(monkeypatch):
    """Start and end each test with no probe in flight.

    The probe is module state that outlives the request that started it, by
    design. A stalled one left behind by a failing test belongs to an event
    loop that pytest-asyncio has since closed, and the next test to wait on
    it would fail for that reason rather than its own.
    """
    monkeypatch.setattr(health, "_probe", None)


async def get_readyz(client, guard: float = HANG_GUARD_SECONDS):
    """GET /readyz, or None if it did not answer within `guard`.

    The guard is asyncio.wait rather than asyncio.timeout or wait_for,
    because both of those bound a call *by cancelling it* -- and a handler
    that does not answer cancellation is exactly what these tests arrange.
    A guard built on cancellation would hang alongside the thing it is
    supposed to be catching.
    """
    request = asyncio.create_task(client.get("/readyz"))

    done, _ = await asyncio.wait({request}, timeout=guard)

    if not done:
        request.cancel()

        return None

    return request.result()


async def retire(stalled) -> None:
    """Let the probe the handler walked away from finish.

    `stalled` is whichever fake is doing the stalling: the connection whose
    query never returned, or the pool that would not take one back.
    """
    stalled.recover()

    await asyncio.sleep(RETIREMENT_SECONDS)


async def test_liveness_holds_with_no_database_at_all(client, monkeypatch):
    """A liveness probe that consults PostgreSQL kills the process for an
    outage that restarting cannot fix."""

    def unreachable():
        raise AssertionError("/healthz must not reach for the database")

    monkeypatch.setattr(health, "get_pool", unreachable)

    assert app.db._pool is None

    response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readiness_holds_when_the_database_answers(client, monkeypatch):
    """The happy path, so the failure tests below prove something."""
    connection = ReadyConnection()
    monkeypatch.setattr(health, "get_pool", lambda: ProbePool(connection))

    response = await client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    assert connection.queries == ["SELECT 1"]


async def test_readiness_fails_when_the_database_is_out_of_reach(
    client, monkeypatch
):
    def unavailable():
        raise RuntimeError("Database pool has not been initialized")

    monkeypatch.setattr(health, "get_pool", unavailable)

    response = await client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"detail": "Service unavailable"}


async def test_readiness_failures_say_nothing_about_the_database(
    client, monkeypatch
):
    """503 is a status, not a diagnostic channel.

    Driver exceptions carry hosts, ports and DSNs, and /readyz is reachable
    by anyone who can reach the service.
    """

    def unavailable():
        raise RuntimeError(f"could not connect to {LEAKY_DETAIL}")

    monkeypatch.setattr(health, "get_pool", unavailable)

    response = await client.get("/readyz")

    assert response.status_code == 503
    assert LEAKY_DETAIL not in response.text
    assert "asyncpg" not in response.text.lower()
    assert "postgres" not in response.text.lower()


async def test_readiness_answers_promptly_when_the_pool_is_exhausted(
    client, monkeypatch
):
    """The probe must report unready rather than wait for a connection.

    Elapsed time is asserted, not just the status code: an endpoint that
    answers 503 after an unbounded wait has failed at the thing readiness is
    for, and only the clock can tell the two apart.
    """
    monkeypatch.setattr(health, "get_pool", lambda: ExhaustedPool())

    started = time.monotonic()
    response = await get_readyz(client)
    elapsed = time.monotonic() - started

    assert response is not None, (
        f"/readyz did not answer within {HANG_GUARD_SECONDS:.0f}s: "
        "acquiring a connection is unbounded"
    )
    assert response.status_code == 503
    assert elapsed < READINESS_TIMEOUT_SECONDS + PROMPTNESS_SLACK_SECONDS


async def test_readiness_answers_when_the_query_will_not_unwind(
    client, monkeypatch
):
    """The probe must not wait on asyncpg's cancellation to complete.

    A bound that works by cancelling the query is no bound at all here: the
    cancellation needs a fresh connection to the server that has stopped
    answering, so it hangs exactly where the query hung. The handler has to
    be able to abandon the probe without waiting to hear that it stopped.

    The cancellation count is asserted too -- the handler should still ask,
    it just must not stay for the answer.
    """
    connection = UnresponsiveConnection()
    monkeypatch.setattr(health, "get_pool", lambda: ProbePool(connection))

    started = time.monotonic()
    response = await get_readyz(client)
    elapsed = time.monotonic() - started

    await retire(connection)

    assert response is not None, (
        f"/readyz did not answer within {HANG_GUARD_SECONDS:.0f}s: the "
        "handler is waiting on a cancellation that cannot complete"
    )
    assert response.status_code == 503
    assert elapsed < READINESS_TIMEOUT_SECONDS + PROMPTNESS_SLACK_SECONDS
    assert connection.cancellations >= 1


async def test_a_stalled_probe_is_joined_rather_than_started_again(
    client, monkeypatch
):
    """Abandoned probes must not accumulate, one per request.

    Every probe in flight holds one of the pool's five connections until it
    unwinds, and an orchestrator re-probes every few seconds. Starting a
    fresh one each time would exhaust the pool through the health check
    itself -- and readiness would then stay broken after the database came
    back, having run out of connections to prove it with.

    The two requests here overlap, which is the cheap way to arrange it;
    a request arriving after an earlier one gave up takes the same branch,
    because giving up does not make a stalled probe done.
    """
    connection = UnresponsiveConnection()
    pool = ProbePool(connection)
    monkeypatch.setattr(health, "get_pool", lambda: pool)

    first, second = await asyncio.gather(
        get_readyz(client), get_readyz(client)
    )

    await retire(connection)

    assert first is not None and second is not None
    assert first.status_code == 503
    assert second.status_code == 503
    assert pool.acquire_count == 1


async def test_readiness_answers_when_the_release_cannot_be_interrupted(
    client, monkeypatch
):
    """Handing the connection back must not be able to hold the answer.

    This is the failure no fake acquire() reproduces: the query succeeds,
    and the handler is caught on the way out instead. Whatever bounds the
    answer has to do it without depending on a cancellation reaching
    asyncpg's shielded release, because nothing guarantees one ever will.
    """
    pool = StrandedReleasePool(ReadyConnection())
    monkeypatch.setattr(health, "get_pool", lambda: pool)

    started = time.monotonic()
    response = await get_readyz(client)
    elapsed = time.monotonic() - started

    await retire(pool)

    assert response is not None, (
        f"/readyz did not answer within {HANG_GUARD_SECONDS:.0f}s: the "
        "handler is held by a release that cannot be interrupted"
    )
    assert response.status_code == 503
    assert elapsed < READINESS_TIMEOUT_SECONDS + PROMPTNESS_SLACK_SECONDS


async def test_the_probe_asks_for_a_bounded_connection(client, monkeypatch):
    """The acquire timeout has to reach asyncpg, not merely exist.

    It is the only thing that bounds the release: asyncpg records the value
    from this call and spends it again giving the connection back
    (pool.py:888-890, then 931-937). Passing nothing records None, and None
    is what leaves a probe holding a connection with nothing able to free
    it.
    """
    pool = ProbePool(ReadyConnection())
    monkeypatch.setattr(health, "get_pool", lambda: pool)

    response = await client.get("/readyz")

    assert response.status_code == 200
    assert pool.acquire_timeouts == [ACQUIRE_TIMEOUT_SECONDS]


def test_the_acquire_budget_leaves_room_to_be_spent_twice():
    """asyncpg charges the acquire timeout once in and once out.

    Both halves have to fit inside the budget the request is answered on,
    or the probe outlives its own answer -- so this is what stops the
    obvious "just pass the whole budget" simplification.
    """
    assert 2 * ACQUIRE_TIMEOUT_SECONDS <= READINESS_TIMEOUT_SECONDS


class _StubSettings:
    """As much of Settings as app.db.connect() reads."""

    def __init__(self, dsn: str):
        self.database_url = SecretStr(dsn)


@pytest.mark.db
async def test_readiness_holds_against_a_real_pool(
    client, postgres_dsn, monkeypatch
):
    """Every pool above is a fake; this one is asyncpg.

    Handing the check to a task and waiting on it rather than awaiting it is
    exactly the kind of change that can satisfy a fake and not a driver, so
    the whole path -- real pool, real acquire, real query -- is exercised
    once. get_pool is deliberately not patched here.
    """
    monkeypatch.setattr(app.db, "_pool", None)
    monkeypatch.setattr(
        app.db, "get_settings", lambda: _StubSettings(postgres_dsn)
    )

    await app.db.connect()

    try:
        started = time.monotonic()
        response = await client.get("/readyz")
        elapsed = time.monotonic() - started
    finally:
        await app.db.disconnect()

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    assert elapsed < READINESS_TIMEOUT_SECONDS


class Blackhole:
    """A TCP relay that can stop moving bytes without closing anything.

    A closed socket is the easy outage: every waiter learns of it at once,
    and asyncpg unwinds fine. The outage that strands a connection is the
    one where the socket stays up and simply stops answering -- a
    partitioned network, a frozen server -- and it is the only way to reach
    the release path under test.

    Once blocked, new connections are accepted and then ignored as well.
    That is not thoroughness: asyncpg cancels a query by opening a new
    connection to the same server (connection.py:1652), and that connection
    hanging is the entire mechanism.
    """

    def __init__(self, host: str, port: int):
        self._upstream = (host, port)
        self._blocked = False
        self._closing = asyncio.Event()
        self._server: asyncio.Server | None = None
        self._writers: list[asyncio.StreamWriter] = []

    @property
    def port(self) -> int:
        return self._server.sockets[0].getsockname()[1]

    def block(self) -> None:
        self._blocked = True

    def unblock(self) -> None:
        self._blocked = False

    async def __aenter__(self):
        self._server = await asyncio.start_server(self._relay, "127.0.0.1", 0)

        return self

    async def __aexit__(self, *exc_info):
        self._closing.set()
        self._server.close()

        for writer in self._writers:
            writer.close()

        # Closed sockets end the relay coroutines; this is the turn they
        # need to notice, so none is left pending when the loop closes.
        await asyncio.sleep(RETIREMENT_SECONDS)

        return False

    async def _relay(self, client_reader, client_writer):
        self._writers.append(client_writer)

        if self._blocked:
            await self._closing.wait()

            return

        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(
                *self._upstream
            )
        except OSError:
            client_writer.close()

            return

        self._writers.append(upstream_writer)

        await asyncio.gather(
            self._pump(client_reader, upstream_writer),
            self._pump(upstream_reader, client_writer),
        )

    async def _pump(self, reader, writer):
        try:
            while True:
                data = await reader.read(65536)

                if not data:
                    return

                if self._blocked:
                    # Neither delivered nor refused. The bytes just stop.
                    continue

                writer.write(data)
                await writer.drain()
        except OSError:
            return


@pytest.mark.db
async def test_an_abandoned_probe_gives_its_connection_back(
    client, postgres_dsn, monkeypatch
):
    """The probe the handler walked away from must still end, and soon.

    Answering promptly is only half the job. The probe holds a pooled
    connection until it unwinds, and unwinding runs asyncpg's release --
    which, given no budget, waits without bound on a cancellation that
    cannot complete against a server that has stopped answering. A /readyz
    that answers in two seconds and strands a connection each time would
    drain a five-connection pool through the health check itself, and then
    have nothing left to prove readiness with once the database came back.

    Real driver, real server, real socket, because the bound under test is
    one asyncpg applies to itself. No fake can show it.
    """
    upstream = urlsplit(postgres_dsn)

    async with Blackhole(upstream.hostname, upstream.port) as blackhole:
        relayed = (
            f"postgresql://{upstream.username}:{upstream.password}"
            f"@127.0.0.1:{blackhole.port}{upstream.path}"
        )

        monkeypatch.setattr(app.db, "_pool", None)
        monkeypatch.setattr(
            app.db, "get_settings", lambda: _StubSettings(relayed)
        )

        await app.db.connect()

        try:
            blackhole.block()

            started = time.monotonic()
            response = await get_readyz(client)
            answered = time.monotonic() - started
            probe = health._probe

            assert response is not None, (
                f"/readyz did not answer within {HANG_GUARD_SECONDS:.0f}s"
            )
            assert response.status_code == 503
            assert answered < READINESS_TIMEOUT_SECONDS + PROMPTNESS_SLACK_SECONDS
            assert probe is not None, (
                "the probe was already finished, so this run proves nothing "
                "about abandoning one"
            )

            done, _ = await asyncio.wait(
                {probe},
                timeout=ACQUIRE_TIMEOUT_SECONDS + PROMPTNESS_SLACK_SECONDS,
            )

            assert done, (
                "the abandoned probe was still holding its connection "
                f"{time.monotonic() - started:.1f}s in; asyncpg's release is "
                "unbounded unless acquire() is given a timeout to record"
            )
        finally:
            blackhole.unblock()

            # Bounded: closing a pool waits for its connections, and this
            # test exists because one of them may not be coming back.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(app.db.disconnect(), HANG_GUARD_SECONDS)


def test_the_readiness_budget_stays_well_under_the_query_timeout():
    """A probe gives up long before a real query is required to.

    Sharing the query budget would make readiness report the database as
    reachable right up to the moment queries start timing out, which is the
    window readiness exists to close.
    """
    assert READINESS_TIMEOUT_SECONDS < COMMAND_TIMEOUT_SECONDS / 2
