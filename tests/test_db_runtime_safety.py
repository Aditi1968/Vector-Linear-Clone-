"""Bounds on every database operation the application performs.

An unbounded query is not merely a slow one: the request, the pooled
connection and the PostgreSQL backend behind it all stay occupied for as
long as it takes, so a single stuck statement costs a fifth of the pool
indefinitely. Two settings close that off, one on each side of the socket,
and neither substitutes for the other -- so both are asserted here.

The client-side bound is asserted on the arguments that actually reach
asyncpg rather than on the constant, because a constant nothing reads is
still a constant. The server-side bound is asserted against a real
PostgreSQL, because passing `server_settings` is not evidence that the
server applied it.
"""

import asyncpg
import pytest
from pydantic import SecretStr

import app.db
from app.db import (
    COMMAND_TIMEOUT_SECONDS,
    STATEMENT_TIMEOUT_MS,
    connect,
    disconnect,
)


# Never dialled: the pool creation it would be used for is faked out.
UNUSED_DSN = "postgresql://vector:PLACEHOLDER@127.0.0.1:5432/vector"

# pg_settings reports the value in the setting's base unit -- milliseconds
# here -- where SHOW would render it as '10s' and force the assertion to
# duplicate PostgreSQL's formatting rules.
STATEMENT_TIMEOUT_QUERY = (
    "SELECT setting FROM pg_settings WHERE name = 'statement_timeout'"
)

# Identifies the backend serving a connection, so a test can tell a reused
# session from a replacement that merely looks like one.
BACKEND_PID_QUERY = "SELECT pg_backend_pid()"

# The shipped ordering with a second's budget instead of ten, and a wider
# gap than the shipped 500ms so a slow host cannot invert it.
SCALED_COMMAND_TIMEOUT_SECONDS = 2.0
SCALED_STATEMENT_TIMEOUT_MS = "500"


class StubSettings:
    """As much of Settings as connect() reads, with no .env involved."""

    def __init__(self, dsn: str):
        self.database_url = SecretStr(dsn)


@pytest.fixture
def recorded_pool_kwargs(monkeypatch) -> dict:
    """What connect() hands to asyncpg.create_pool, with no server dialled.

    `_pool` is monkeypatched rather than assigned so that the sentinel this
    leaves behind is restored to None on teardown; other tests assert the
    module starts with no pool.
    """
    recorded: dict = {}

    async def fake_create_pool(**kwargs):
        recorded.update(kwargs)

        return object()

    monkeypatch.setattr(app.db, "_pool", None)
    monkeypatch.setattr(app.db, "get_settings", lambda: StubSettings(UNUSED_DSN))
    monkeypatch.setattr(asyncpg, "create_pool", fake_create_pool)

    return recorded


async def test_the_client_stops_waiting_after_ten_seconds(recorded_pool_kwargs):
    """command_timeout reaches the driver, not just the module namespace."""
    await connect()

    assert recorded_pool_kwargs["command_timeout"] == 10.0
    assert recorded_pool_kwargs["command_timeout"] == COMMAND_TIMEOUT_SECONDS


async def test_the_server_is_given_a_deadline_of_its_own(recorded_pool_kwargs):
    """statement_timeout is configured for connections, not per call site.

    Set per query it would be forgotten at the first new call site; set on
    the pool it applies to everything the application ever executes.
    """
    await connect()

    assert recorded_pool_kwargs["server_settings"] == {"statement_timeout": "9500"}


def test_the_server_gives_up_before_the_client_does():
    """The ordering of the two bounds, which is the point of having both.

    Equal values would make this pair pure decoration: the client bound
    wins a tie, so statement_timeout could never fire and PostgreSQL would
    never be the one to end a runaway query. That matters beyond
    tidiness -- when the client gives up first, asyncpg has to open a
    second connection to the same server to send a CancelRequest
    (connection.py:1652), which is the round trip that cannot complete
    against an unreachable server and that strands a pooled connection in
    release. Ordering the server bound underneath keeps that path
    exceptional.

    Pinned here so that "these should both just be 10 seconds" fails
    instead of quietly disarming the server-side bound.
    """
    assert int(STATEMENT_TIMEOUT_MS) / 1000 < COMMAND_TIMEOUT_SECONDS


async def test_the_pool_still_holds_five_connections(recorded_pool_kwargs):
    """Pool size is a capacity decision and stays out of the safety story.

    Widening the pool is the tempting response to exhaustion and it fixes
    nothing: it buys a larger number of connections to exhaust while raising
    the load a struggling database is asked to carry. Pinned so that change
    has to be made deliberately.
    """
    await connect()

    assert recorded_pool_kwargs["max_size"] == 5
    assert recorded_pool_kwargs["min_size"] == 1


@pytest.mark.db
async def test_postgres_reports_the_statement_timeout_on_pooled_connections(
    postgres_dsn, monkeypatch
):
    """PostgreSQL itself confirms the timeout, on the very same backend.

    The reuse is the point. asyncpg runs RESET ALL when a connection returns
    to the pool, so a timeout applied with SET from an `init` coroutine would
    hold for the first acquire and be gone from every one after it. Sending
    it in the startup packet makes it the session default that RESET ALL
    restores to -- which is a claim about the mechanism, and this is the
    assertion that tests it.

    Reading the setting twice proves nothing on its own: a brand-new
    connection carries the same startup-packet value, so the assertion would
    hold whether or not anything survived RESET ALL. The backend PID is what
    makes it a test of session survival, and a differing one fails loudly
    rather than passing vacuously.
    """
    monkeypatch.setattr(app.db, "_pool", None)
    monkeypatch.setattr(app.db, "get_settings", lambda: StubSettings(postgres_dsn))

    await connect()

    try:
        pool = app.db.get_pool()

        async with pool.acquire() as connection:
            first_backend = await connection.fetchval(BACKEND_PID_QUERY)
            on_first_use = await connection.fetchval(STATEMENT_TIMEOUT_QUERY)

        async with pool.acquire() as connection:
            second_backend = await connection.fetchval(BACKEND_PID_QUERY)
            after_release = await connection.fetchval(STATEMENT_TIMEOUT_QUERY)
    finally:
        await disconnect()

    assert second_backend == first_backend, (
        f"the pool handed out backend {second_backend} rather than reusing "
        f"{first_backend}, so the second read says nothing about whether a "
        "session survived RESET ALL"
    )
    assert on_first_use == STATEMENT_TIMEOUT_MS
    assert after_release == STATEMENT_TIMEOUT_MS


@pytest.mark.db
async def test_the_server_and_not_the_client_ends_a_runaway_query(postgres_dsn):
    """Which side reports the timeout, which is what the ordering buys.

    A client-side TimeoutError would mean asyncpg had gone off to open a
    second connection to send a CancelRequest. QueryCanceledError means
    PostgreSQL ended the statement itself and said so over the connection
    already open. The pair is ordered to make the second one the normal
    path, and this asserts that the ordering actually produces it.

    Scaled down from the shipped pair, and with a wider gap, so it costs
    about a second instead of ten and does not turn a slow moment on the
    host into a flake. Only the ordering is under test; the shipped values
    are pinned by test_the_server_gives_up_before_the_client_does.
    """
    pool = await asyncpg.create_pool(
        dsn=postgres_dsn,
        min_size=1,
        max_size=1,
        command_timeout=SCALED_COMMAND_TIMEOUT_SECONDS,
        server_settings={"statement_timeout": SCALED_STATEMENT_TIMEOUT_MS},
    )

    try:
        async with pool.acquire() as connection:
            with pytest.raises(asyncpg.exceptions.QueryCanceledError):
                await connection.fetchval("SELECT pg_sleep(5)")
    finally:
        await pool.close()
