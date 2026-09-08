"""Migration 032 and the sweep, against a real PostgreSQL.

tests/test_auth_service.py reasons about the limiter through a fake, and a
fake cannot answer any of the questions that actually decide whether a rate
limit is a rate limit, because every one of them is a question about the
server:

  * WHETHER TWO REPLICAS COUNTING AT THE SAME INSTANT REACH 2. Not "usually":
    the test below holds one transaction open while the other increments, so
    the serialisation is forced rather than hoped for. A SELECT-then-UPDATE
    would lose the update here, and would lose it precisely under the
    concurrency an attacker supplies for free;
  * WHETHER THE WINDOW ROLLS INSIDE THE STATEMENT, which is a CASE in an
    upsert and is decided by `now()` rather than by any Python this repository
    contains;
  * WHETHER THE CHECK CONSTRAINTS ARE REAL. An empty subject would collapse
    every anonymous request into one bucket and lock out the whole deployment,
    quietly, and a constraint that reads as correct in a migration is exactly
    the kind of thing that turns out to have been enforced by nothing;
  * WHETHER AN EXPIRED SESSION IS ACTUALLY COLLECTED. `sessions` only ever
    grew: migrations/003_auth.sql created `sessions_expires_at_idx` for a
    sweep it correctly predicted and that nobody wrote until now;
  * AND WHETHER AN ADDRESS IS ANYWHERE IN THE TABLE, asked by rendering whole
    rows as text and searching them.

Marked `db`: deselected by default, skipped when Docker is unreachable.
"""

import asyncio
from datetime import timedelta

import asyncpg
import pytest

from app.domain.errors import AuthenticationError
from app.repositories.rate_limits import RateLimitRepository
from app.repositories.sessions import SessionRepository
from app.repositories.users import UserRepository
from app.services.auth import (
    LOGIN_BUDGETS,
    LOGIN_EMAIL_SCOPE,
    LOGIN_IP_SCOPE,
    RATE_LIMIT_WINDOW,
    REGISTER_IP_SCOPE,
    AuthService,
    _subject_for_email,
)
from app.services.passwords import Argon2PasswordHasher

from tests.conftest import apply_all_migrations, reset_schema


pytestmark = pytest.mark.db

EMAIL = "ada@example.com"
PASSWORD = "correct horse battery staple"
CLIENT_IP = "203.0.113.7"

# Whole rows as text, which is how a search for a leaked secret stays correct
# when a column is added. Checking named columns would quietly stop covering
# the new one. The same idiom tests/test_auth_db.py uses on `users`.
LIMITS_AS_TEXT = "SELECT auth_rate_limits::text FROM auth_rate_limits"


@pytest.fixture
async def pool(postgres_dsn):
    """A pool onto a schema every migration has just been applied to.

    Rebuilt per test rather than shared: these tests are about counting, where
    a row left behind by the previous test does not add noise, it changes the
    answer.
    """
    connection = await asyncpg.connect(postgres_dsn)

    try:
        await reset_schema(connection)
        await apply_all_migrations(connection)
    finally:
        await connection.close()

    created = await asyncpg.create_pool(dsn=postgres_dsn, min_size=1, max_size=4)

    try:
        yield created
    finally:
        await created.close()


@pytest.fixture
def limits() -> RateLimitRepository:
    return RateLimitRepository()


@pytest.fixture
def service(pool) -> AuthService:
    """The real service over the real repositories over the real database.

    Real argon2 too, which is why the tests here that drive a whole budget to
    its edge are few: ten verifications is a second of wall clock, and the
    arithmetic of the budget is already proved in tests/test_auth_service.py
    against a fake. What is proved here is what only a server can answer.
    """
    return AuthService(
        pool=pool,
        users=UserRepository(),
        sessions=SessionRepository(),
        hasher=Argon2PasswordHasher(),
        rate_limits=RateLimitRepository(),
    )


LOGIN_BUCKETS = [
    (LOGIN_EMAIL_SCOPE, _subject_for_email(EMAIL)),
    (LOGIN_IP_SCOPE, CLIENT_IP),
]


# --- the counter itself -------------------------------------------------


async def test_the_migration_builds_the_table_the_repository_addresses(pool, limits):
    """The whole point of applying it through the runner: it executes."""
    async with pool.acquire() as connection:
        attempts = await limits.consume(
            connection,
            buckets=LOGIN_BUCKETS,
            window=RATE_LIMIT_WINDOW,
        )

    assert attempts == {LOGIN_EMAIL_SCOPE: 1, LOGIN_IP_SCOPE: 1}


async def test_repeated_attempts_increment_in_place(pool, limits):
    """One row per bucket, counting up -- not one row per attempt.

    This is what makes the table's size a function of who is active rather
    than of how much traffic arrived, which is the trade migration 032 takes
    against an exact sliding window.
    """
    async with pool.acquire() as connection:
        for _ in range(3):
            attempts = await limits.consume(
                connection,
                buckets=LOGIN_BUCKETS,
                window=RATE_LIMIT_WINDOW,
            )

        rows = await connection.fetchval("SELECT count(*) FROM auth_rate_limits")

    assert attempts == {LOGIN_EMAIL_SCOPE: 3, LOGIN_IP_SCOPE: 3}
    assert rows == 2


async def test_two_replicas_counting_at_the_same_instant_reach_two(pool, limits):
    """The claim the whole table rests on, forced rather than raced for.

    `ON CONFLICT DO UPDATE` takes the row lock BEFORE it reads `attempts`, so
    two processes incrementing one subject serialise into 1 then 2. The
    alternative implementation -- SELECT the count, decide, UPDATE it -- reads
    1 twice and writes 1 twice, and an attacker gets both attempts for the
    price of one by simply opening two connections.

    The overlap is FORCED, not hoped for. The first connection's transaction is
    held open while the second's upsert is started as a task; the assertion
    that the task has not finished is the proof it is parked on the row lock,
    and only then is the first committed. Two statements run one after the
    other would pass whatever the implementation did, which is why the pending
    check is here rather than a comment saying they overlapped.
    """
    first = await pool.acquire()
    second = await pool.acquire()

    try:
        transaction = first.transaction()
        await transaction.start()

        await limits.consume(first, buckets=LOGIN_BUCKETS, window=RATE_LIMIT_WINDOW)

        pending = asyncio.create_task(
            limits.consume(second, buckets=LOGIN_BUCKETS, window=RATE_LIMIT_WINDOW)
        )

        # Long enough for a statement that was NOT going to block to have
        # finished several times over. If the upsert took no row lock, this
        # assertion is what fails -- and it fails before the interesting one,
        # so the report names the actual defect.
        await asyncio.sleep(0.5)

        assert not pending.done(), (
            "the second upsert did not block on the first's row lock, so two "
            "replicas would each count 1 for the same attempt"
        )

        await transaction.commit()

        attempts = await pending
    finally:
        await pool.release(first)
        await pool.release(second)

    assert attempts == {LOGIN_EMAIL_SCOPE: 2, LOGIN_IP_SCOPE: 2}


async def test_a_bucket_older_than_the_window_restarts_at_one(pool, limits):
    """The rollover, which happens inside the upsert and needs no sweep.

    A bucket whose window opened longer ago than the interval is not "expired
    and waiting to be collected" -- it restarts at 1 on the next attempt, with
    a fresh `window_started_at`. That is what lets the sweep fall arbitrarily
    far behind without changing a single decision.

    The window is aged by moving the row's timestamp rather than by sleeping,
    because a test that sleeps for its subject's window is a test nobody runs.
    """
    async with pool.acquire() as connection:
        for _ in range(5):
            await limits.consume(
                connection, buckets=LOGIN_BUCKETS, window=RATE_LIMIT_WINDOW
            )

        await connection.execute(
            """
            UPDATE auth_rate_limits
            SET window_started_at = now() - interval '1 hour'
            """
        )

        attempts = await limits.consume(
            connection,
            buckets=LOGIN_BUCKETS,
            window=RATE_LIMIT_WINDOW,
        )

        opened = await connection.fetchval(
            "SELECT min(window_started_at) FROM auth_rate_limits"
        )
        now = await connection.fetchval("SELECT now()")

    assert attempts == {LOGIN_EMAIL_SCOPE: 1, LOGIN_IP_SCOPE: 1}
    assert now - opened < timedelta(minutes=1)


async def test_a_bucket_inside_the_window_does_not_restart(pool, limits):
    """The other side of the same CASE.

    A rollover predicate one comparison out -- `>=` for `<=`, or a window
    measured from the wrong column -- would reset every bucket on every
    attempt, which is a limiter that counts to one forever and reads as
    working.
    """
    async with pool.acquire() as connection:
        await limits.consume(
            connection, buckets=LOGIN_BUCKETS, window=RATE_LIMIT_WINDOW
        )

        await connection.execute(
            """
            UPDATE auth_rate_limits
            SET window_started_at = now() - interval '5 minutes'
            """
        )

        attempts = await limits.consume(
            connection,
            buckets=LOGIN_BUCKETS,
            window=RATE_LIMIT_WINDOW,
        )

    assert attempts == {LOGIN_EMAIL_SCOPE: 2, LOGIN_IP_SCOPE: 2}


async def test_clearing_a_bucket_removes_the_row_rather_than_zeroing_it(pool, limits):
    """Absence IS zero, and `auth_rate_limits_attempts_positive` says so.

    A reset to 0 would need a row that the CHECK forbids, so the constraint and
    the DELETE are one design: there is no representation of "this subject has
    made no attempts" other than having no row.
    """
    async with pool.acquire() as connection:
        await limits.consume(
            connection, buckets=LOGIN_BUCKETS, window=RATE_LIMIT_WINDOW
        )
        await limits.clear(connection, buckets=LOGIN_BUCKETS)

        remaining = await connection.fetchval("SELECT count(*) FROM auth_rate_limits")

    assert remaining == 0


async def test_clearing_one_bucket_leaves_another_subject_alone(pool, limits):
    """A DELETE whose predicate lost a column would clear the whole table.

    That is not a smaller bug than failing to clear: it is every attacker's
    budget refunded by any user anywhere signing in successfully.
    """
    other = [(LOGIN_EMAIL_SCOPE, _subject_for_email("someone@example.com"))]

    async with pool.acquire() as connection:
        await limits.consume(
            connection, buckets=LOGIN_BUCKETS, window=RATE_LIMIT_WINDOW
        )
        await limits.consume(connection, buckets=other, window=RATE_LIMIT_WINDOW)

        await limits.clear(connection, buckets=LOGIN_BUCKETS)

        rows = await connection.fetch("SELECT scope, subject FROM auth_rate_limits")

    assert [(row["scope"], row["subject"]) for row in rows] == other


async def test_no_address_is_anywhere_in_the_table(pool, service):
    """Asked of whole rows, not of the column this test happens to know about.

    The limiter is written by unauthenticated traffic against addresses that
    mostly have no account, so a plaintext column here would be a list of every
    address anyone ever guessed at -- in a table with no tenant and no foreign
    key. `_subject_for_email` is the only thing that builds a subject, and this
    is what proves nothing routes around it.
    """
    with pytest.raises(AuthenticationError):
        await service.log_in(email=EMAIL, password="wrong", client_ip=CLIENT_IP)

    async with pool.acquire() as connection:
        rendered = "\n".join(
            record[0] for record in await connection.fetch(LIMITS_AS_TEXT)
        )

    assert rendered
    assert EMAIL not in rendered
    assert "ada" not in rendered
    assert _subject_for_email(EMAIL) in rendered


# --- the constraints ----------------------------------------------------


@pytest.mark.parametrize(
    ("scope", "subject"),
    [
        ("", "203.0.113.7"),
        ("login:ip", ""),
        ("login:ip", "x" * 129),
        ("s" * 65, "203.0.113.7"),
    ],
)
async def test_a_malformed_bucket_key_is_refused_by_the_database(pool, scope, subject):
    """The backstop under `app/http_client_ip.py`, and a category of bug.

    Both halves of the key are supplied by the application, so these are not
    input validation -- they are the guarantee that a caller which ever passed
    an empty subject fails loudly instead of collapsing every anonymous request
    into one shared bucket and locking out the deployment quietly.
    """
    async with pool.acquire() as connection:
        with pytest.raises(asyncpg.CheckViolationError):
            await connection.execute(
                """
                INSERT INTO auth_rate_limits (
                    scope, subject, window_started_at, attempts
                )
                VALUES ($1, $2, now(), 1)
                """,
                scope,
                subject,
            )


async def test_a_bucket_with_no_attempts_is_refused(pool):
    """There is no row for zero; see `clear`."""
    async with pool.acquire() as connection:
        with pytest.raises(asyncpg.CheckViolationError):
            await connection.execute(
                """
                INSERT INTO auth_rate_limits (
                    scope, subject, window_started_at, attempts
                )
                VALUES ('login:ip', '203.0.113.7', now(), 0)
                """
            )


async def test_the_longest_real_subject_fits(pool):
    """A digest is 64 characters and the longest address is 45.

    A column capped one character short would refuse the very keys the limiter
    produces, and would refuse them as a 500 on the log-in path.
    """
    async with pool.acquire() as connection:
        await connection.execute(
            """
            INSERT INTO auth_rate_limits (
                scope, subject, window_started_at, attempts
            )
            VALUES ($1, $2, now(), 1)
            """,
            LOGIN_EMAIL_SCOPE,
            _subject_for_email(EMAIL),
        )

        stored = await connection.fetchval("SELECT subject FROM auth_rate_limits")

    assert len(stored) == 64


# --- the sweep ----------------------------------------------------------


async def test_an_expired_session_is_collected_by_the_sweep(pool, service):
    """The defect migrations/003_auth.sql predicted, closed.

    `sessions` had one row per log-in, forever, each holding the digest of a
    credential that stopped meaning anything a fortnight ago -- and 003 created
    `sessions_expires_at_idx` for the sweep that was going to fix it. This is
    that sweep, deleting a row a server decided was expired.

    Expiry is aged by moving `expires_at` rather than by issuing a session and
    waiting fourteen days, but the DECISION is still the database's: the
    predicate is `expires_at <= now()` in the DELETE, on the same clock
    `touch_valid` compares against.
    """
    registered = await service.register(
        email=EMAIL,
        password=PASSWORD,
        name=None,
        client_ip=CLIENT_IP,
    )

    async with pool.acquire() as connection:
        await connection.execute(
            "UPDATE sessions SET expires_at = now() - interval '1 second'"
        )

    collected_sessions, _ = await service.sweep_once()

    assert collected_sessions == 1

    async with pool.acquire() as connection:
        assert await connection.fetchval("SELECT count(*) FROM sessions") == 0

    # And the token it stood for identifies nobody, which was already true
    # before the sweep -- the sweep reclaims space, it does not revoke.
    assert await service.authenticate(registered.issued.token) is None


async def test_a_live_session_survives_the_sweep(pool, service):
    """The failure that would look like a product randomly signing people out.

    A predicate one comparison out -- `>=` for `<=`, or a cutoff computed on
    the application's clock rather than the server's -- deletes sessions this
    database still considers live, and does it silently.
    """
    registered = await service.register(
        email=EMAIL,
        password=PASSWORD,
        name=None,
        client_ip=CLIENT_IP,
    )

    collected_sessions, _ = await service.sweep_once()

    assert collected_sessions == 0
    assert await service.authenticate(registered.issued.token) is not None


async def test_the_sweep_collects_aged_out_buckets_and_leaves_fresh_ones(pool, service):
    """What `auth_rate_limits_window_idx` was created for.

    Only the aged-out row goes. A sweep that took the fresh one would be
    refunding budget on a timer, which is the limiter quietly not limiting.
    """
    async with pool.acquire() as connection:
        limits = RateLimitRepository()

        await limits.consume(
            connection, buckets=LOGIN_BUCKETS, window=RATE_LIMIT_WINDOW
        )

        # Only the address bucket is aged, so the IP bucket is the control.
        await connection.execute(
            """
            UPDATE auth_rate_limits
            SET window_started_at = now() - interval '1 hour'
            WHERE scope = $1
            """,
            LOGIN_EMAIL_SCOPE,
        )

    _, collected_limits = await service.sweep_once()

    assert collected_limits == 1

    async with pool.acquire() as connection:
        remaining = await connection.fetch("SELECT scope FROM auth_rate_limits")

    assert [row["scope"] for row in remaining] == [LOGIN_IP_SCOPE]


async def test_a_swept_bucket_costs_the_attacker_nothing_it_had_not_already_spent(
    pool, service
):
    """The sweep is space, and this is the claim stated as a test.

    A collected bucket is one whose window had already rolled, so the attempt
    that follows it would have restarted at 1 whether the row was there or not.
    If this ever stops being true, the sweep has become a way to refund a live
    budget and the interval becomes a security parameter rather than a
    housekeeping one.
    """
    limits = RateLimitRepository()

    async with pool.acquire() as connection:
        for _ in range(LOGIN_BUDGETS[LOGIN_EMAIL_SCOPE]):
            await limits.consume(
                connection,
                buckets=LOGIN_BUCKETS,
                window=RATE_LIMIT_WINDOW,
            )

        await connection.execute(
            "UPDATE auth_rate_limits SET window_started_at = now() - interval '1 hour'"
        )

        # What the next attempt would count WITHOUT a sweep having run.
        without_sweep = await limits.consume(
            connection,
            buckets=LOGIN_BUCKETS,
            window=RATE_LIMIT_WINDOW,
        )

        await connection.execute("DELETE FROM auth_rate_limits")

        with_sweep = await limits.consume(
            connection,
            buckets=LOGIN_BUCKETS,
            window=RATE_LIMIT_WINDOW,
        )

    assert without_sweep == with_sweep == {LOGIN_EMAIL_SCOPE: 1, LOGIN_IP_SCOPE: 1}


async def test_the_sweep_is_bounded_by_its_batch(pool, service):
    """One transaction per batch, and the pass loops rather than stopping.

    An unbounded DELETE on a table nobody has ever swept is one transaction
    holding row locks over every dead row in it -- bloating WAL and blocking
    the `touch_valid` UPDATE every authenticated request makes -- and the first
    time it matters is the first time it is slow.
    """
    async with pool.acquire() as connection:
        await connection.execute(
            """
            INSERT INTO auth_rate_limits (
                scope, subject, window_started_at, attempts
            )
            SELECT 'login:ip', '192.0.2.' || generated, now() - interval '1 hour', 1
            FROM generate_series(1, 5) AS generated
            """
        )

    _, collected_limits = await service.sweep_once(batch=2)

    assert collected_limits == 5

    async with pool.acquire() as connection:
        assert await connection.fetchval("SELECT count(*) FROM auth_rate_limits") == 0


async def test_two_sweeps_racing_leave_the_table_in_one_state(pool, service):
    """No lease, no advisory lock, no SKIP LOCKED -- and it is still correct.

    migrations 027 and 028 lease a row so exactly one worker performs an
    expensive, external, NON-idempotent action with it. Here the statement IS
    the work and a row deleted twice is a row deleted, so two replicas racing
    take locks in physical order and both return having left the table in the
    one state either intended. The only thing a lock would buy is a tidier
    number in the log line -- which is why the totals below are summed rather
    than asserted individually.
    """
    async with pool.acquire() as connection:
        await connection.execute(
            """
            INSERT INTO auth_rate_limits (
                scope, subject, window_started_at, attempts
            )
            SELECT 'login:ip', '192.0.2.' || generated, now() - interval '1 hour', 1
            FROM generate_series(1, 20) AS generated
            """
        )

    first, second = await asyncio.gather(
        service.sweep_once(batch=3),
        service.sweep_once(batch=3),
    )

    assert first[1] + second[1] == 20

    async with pool.acquire() as connection:
        assert await connection.fetchval("SELECT count(*) FROM auth_rate_limits") == 0


# --- the limiter through the real service -------------------------------


async def test_the_budget_refuses_a_correct_password_once_it_is_spent(pool, service):
    """End to end, with real argon2 and a real table.

    The last attempt uses the RIGHT password and is still refused, which is the
    lockout this design accepts written down as an executable fact rather than
    as a paragraph in a comment. It expires with the window and a successful
    log-in clears it; neither of those helps the user in this moment, and that
    is the trade.
    """
    await service.register(
        email=EMAIL,
        password=PASSWORD,
        name=None,
        client_ip=CLIENT_IP,
    )

    # Registration issued a session and spent no login budget, so the whole
    # address budget is still here to be spent.
    for _ in range(LOGIN_BUDGETS[LOGIN_EMAIL_SCOPE]):
        with pytest.raises(AuthenticationError):
            await service.log_in(email=EMAIL, password="wrong", client_ip=CLIENT_IP)

    with pytest.raises(AuthenticationError):
        await service.log_in(email=EMAIL, password=PASSWORD, client_ip=CLIENT_IP)

    # And the window is what ends it -- not an administrator, and not a
    # successful log-in, which cannot happen while it is refused.
    async with pool.acquire() as connection:
        await connection.execute(
            "UPDATE auth_rate_limits SET window_started_at = now() - interval '1 hour'"
        )

    signed_in = await service.log_in(
        email=EMAIL,
        password=PASSWORD,
        client_ip=CLIENT_IP,
    )

    assert signed_in.user.email == EMAIL

    # And that success cleared what the LOG-IN spent, so the next fumble starts
    # from one rather than from the edge.
    #
    # The registration bucket survives, and must: a successful log-in says
    # nothing about how many accounts this address has been opening, and a
    # `clear` that took every scope would let anyone reset their enumeration
    # budget by signing in to an account they already have.
    async with pool.acquire() as connection:
        remaining = await connection.fetch("SELECT scope FROM auth_rate_limits")

    assert [row["scope"] for row in remaining] == [REGISTER_IP_SCOPE]
