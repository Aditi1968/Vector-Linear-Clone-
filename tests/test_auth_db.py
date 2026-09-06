"""Authentication against a real PostgreSQL 18, with real argon2.

Everything else in this suite reasons about auth through fakes, and fakes
cannot answer the questions that matter most here, because every one of them
is a question about the server:

  * whether two spellings of one address can both exist -- which is a claim
    about a CHECK constraint and a UNIQUE index working together, and is
    exactly the kind of thing that reads as correct in a migration and turns
    out to have been enforced by nothing;
  * whether an expired session is refused -- which is decided by `now()` in a
    WHERE clause, not by any Python this repository contains;
  * whether deleting a user really takes their sessions with them, which is
    one word in a foreign key and cannot be observed without a server;
  * and whether the raw token, having gone through hashing, the repository,
    the wire protocol and the storage layer, is genuinely absent from the
    table -- asked by rendering whole rows as text and searching them.

The schema comes from `migrations/003_auth.sql`, applied through
`scripts.apply_migration` exactly as an operator would, so this file also
proves the migration executes at all.

Marked `db`: deselected by default, skipped when Docker is unreachable.
"""

from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from app.domain.errors import AuthenticationError, ValidationError
from app.repositories.sessions import SessionRepository
from app.repositories.users import UserRepository
from app.services.auth import AuthService
from app.services.passwords import Argon2PasswordHasher
from app.services.tokens import generate_session_token, hash_session_token
from scripts.apply_migration import apply_migration

from tests.conftest import reset_schema


pytestmark = pytest.mark.db

MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "003_auth.sql"
MIGRATIONS_DIR = MIGRATION.parent

EMAIL = "ada@example.com"
PASSWORD = "correct horse battery staple"
NAME = "Ada Lovelace"

# A recognisable, well-formed argon2id hash to insert directly when a test is
# about the schema rather than about hashing. Real, so that
# users_password_hash_argon2id accepts it; a constant, so that no test pays
# 100ms to produce one it never verifies.
STORED_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$"
    "B+uteoqvIScxtHiGUNLG8g$DUXzD54RXGyYbin0zmwql6nYUtjK9AP/fpPNiMVif6s"
)

INSERT_USER = """
INSERT INTO users (email, password_hash, name)
VALUES ($1, $2, $3)
RETURNING id
"""

INSERT_SESSION = """
INSERT INTO sessions (user_id, token_hash, expires_at)
VALUES ($1, $2, now() + interval '1 day')
"""

# Whole rows as text, which is how a search for a leaked secret stays
# correct when a column is added. Checking named columns would quietly stop
# covering the new one.
USERS_AS_TEXT = "SELECT users::text FROM users"
SESSIONS_AS_TEXT = "SELECT sessions::text FROM sessions"


async def apply_auth_migration(connection: asyncpg.Connection) -> str:
    """Run 003 the way an operator would: through the runner, in one
    transaction the caller owns."""
    async with connection.transaction():
        return await apply_migration(
            connection,
            MIGRATION,
            migrations_dir=MIGRATIONS_DIR,
        )


@pytest.fixture
async def prepared(postgres_dsn):
    """A pool onto an empty schema that 003 has just been applied to.

    Rebuilt per test rather than shared, so that one test's rows cannot
    decide another's result -- these tests are about uniqueness and
    cascades, where leftover rows do not merely add noise, they change the
    answer.
    """
    connection = await asyncpg.connect(postgres_dsn)

    try:
        await reset_schema(connection)
        await apply_auth_migration(connection)
    finally:
        await connection.close()

    pool = await asyncpg.create_pool(dsn=postgres_dsn, min_size=1, max_size=2)

    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
def service(prepared) -> AuthService:
    """The real service over the real repositories over the real database.

    Real argon2 too. Every registration in this file costs one hash and
    every log-in costs one verification, which is the price of testing the
    thing that ships.
    """
    return AuthService(
        pool=prepared,
        users=UserRepository(),
        sessions=SessionRepository(),
        hasher=Argon2PasswordHasher(),
    )


async def rows_as_text(pool, query: str) -> str:
    async with pool.acquire() as connection:
        return "\n".join(record[0] for record in await connection.fetch(query))


# --- the migration itself ----------------------------------------------


async def test_the_migration_applies_and_is_recorded_in_the_ledger(postgres_dsn):
    """Applied once, and refused the second time.

    The ledger is what makes "003 has run" a fact rather than a memory, and
    a runner that happily re-executed a migration would make every later
    decision rest on nothing.
    """
    connection = await asyncpg.connect(postgres_dsn)

    try:
        await reset_schema(connection)

        first = await apply_auth_migration(connection)
        second = await apply_auth_migration(connection)

        applied = await connection.fetchval(
            "SELECT count(*) FROM schema_migrations WHERE version = '003'"
        )
    finally:
        await connection.close()

    assert "Applied migration 003" in first
    assert "already applied" in second
    assert applied == 1


async def test_the_migration_creates_both_tables_and_nothing_else(prepared):
    async with prepared.acquire() as connection:
        tables = await connection.fetch(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
            ORDER BY table_name
            """
        )

    # schema_migrations is the runner's ledger, not part of 003.
    assert [row["table_name"] for row in tables] == [
        "schema_migrations",
        "sessions",
        "users",
    ]


# --- what the schema refuses to store ----------------------------------


async def test_an_address_is_stored_only_in_lowercase(prepared):
    """users_email_lowercase, which is what makes a plain UNIQUE enough.

    Without it, 'Ada@Example.com' and 'ada@example.com' are two rows that
    both insert cleanly, and a client varying capitalisation addresses a
    different account while every uniqueness check reports success.
    """
    async with prepared.acquire() as connection:
        with pytest.raises(asyncpg.CheckViolationError) as raised:
            await connection.execute(
                INSERT_USER,
                "Ada@Example.com",
                STORED_HASH,
                NAME,
            )

    assert raised.value.constraint_name == "users_email_lowercase"


async def test_one_address_cannot_be_registered_twice(prepared):
    async with prepared.acquire() as connection:
        await connection.execute(INSERT_USER, EMAIL, STORED_HASH, NAME)

        with pytest.raises(asyncpg.UniqueViolationError) as raised:
            await connection.execute(INSERT_USER, EMAIL, STORED_HASH, "Someone Else")

    assert raised.value.constraint_name == "users_email_key"


@pytest.mark.parametrize(
    "email",
    [
        "no-at-sign",
        "@example.com",
        "ada@",
        "ada@two@example.com",
        "ada example@example.com",
        "ada@exa mple.com",
    ],
)
async def test_an_address_that_is_certainly_not_one_is_refused(prepared, email):
    """The floor beneath the service's validation, not a replacement for it.

    A row can never hold something no mail system could accept, whatever
    path wrote it -- a fixture, a console, a repository method nobody has
    written yet.
    """
    async with prepared.acquire() as connection:
        with pytest.raises(asyncpg.CheckViolationError) as raised:
            await connection.execute(INSERT_USER, email, STORED_HASH, None)

    assert raised.value.constraint_name in {
        "users_email_shape",
        "users_email_length",
    }


@pytest.mark.parametrize(
    "password_hash",
    [
        # The failure this constraint exists for: a code path that stored the
        # password itself. It would be unnoticeable in any test that only
        # checked log-in still worked.
        PASSWORD,
        "",
        "$argon2i$v=19$m=65536,t=3,p=4$c2FsdHNhbHQ$aGFzaGhhc2g",
        "$2b$12$KIXQm2Nb0BJ2mE5rGZ0/8u9CqUOaR0F1Z8yv0dGm0k1cQO5vJm6Yy",
    ],
)
async def test_password_hash_holds_nothing_but_an_argon2id_hash(
    prepared, password_hash
):
    async with prepared.acquire() as connection:
        with pytest.raises(asyncpg.CheckViolationError) as raised:
            await connection.execute(INSERT_USER, EMAIL, password_hash, None)

    assert raised.value.constraint_name == "users_password_hash_argon2id"


@pytest.mark.parametrize("digest", [b"", b"short", b"x" * 31, b"x" * 33])
async def test_a_token_hash_must_be_a_full_sha256_digest(prepared, digest):
    """Anything else in the column is some other function's output.

    A truncated digest, or a hex string stored as bytes, would still look
    like a hash and would still be looked up successfully -- with a fraction
    of the collision resistance the column is assumed to have.
    """
    async with prepared.acquire() as connection:
        user_id = await connection.fetchval(INSERT_USER, EMAIL, STORED_HASH, None)

        with pytest.raises(asyncpg.CheckViolationError) as raised:
            await connection.execute(INSERT_SESSION, user_id, digest)

    assert raised.value.constraint_name == "sessions_token_hash_length"


async def test_two_sessions_cannot_share_a_digest(prepared):
    """Unreachable in practice, refused anyway.

    Two live sessions with one digest would authenticate each other's owner.
    A 256-bit token makes that impossible rather than unlikely, which is
    exactly why the database should be the one asserting it and not a
    comment.
    """
    digest = hash_session_token(generate_session_token())

    async with prepared.acquire() as connection:
        first = await connection.fetchval(INSERT_USER, EMAIL, STORED_HASH, None)
        second = await connection.fetchval(
            INSERT_USER, "grace@example.com", STORED_HASH, None
        )

        await connection.execute(INSERT_SESSION, first, digest)

        with pytest.raises(asyncpg.UniqueViolationError) as raised:
            await connection.execute(INSERT_SESSION, second, digest)

    assert raised.value.constraint_name == "sessions_token_hash_key"


async def test_deleting_a_user_takes_their_sessions_with_them(prepared):
    """ON DELETE CASCADE, and the reason it is not RESTRICT here.

    A session is derived state, worthless once its user is gone. RESTRICT
    would make deleting an account fail until every session had been removed
    by hand, and a half-finished account deletion that leaves live sessions
    behind is the worse outcome by a distance.
    """
    async with prepared.acquire() as connection:
        user_id = await connection.fetchval(INSERT_USER, EMAIL, STORED_HASH, None)

        await connection.execute(
            INSERT_SESSION,
            user_id,
            hash_session_token(generate_session_token()),
        )
        await connection.execute(
            INSERT_SESSION,
            user_id,
            hash_session_token(generate_session_token()),
        )

        assert await connection.fetchval("SELECT count(*) FROM sessions") == 2

        await connection.execute("DELETE FROM users WHERE id = $1", user_id)

        assert await connection.fetchval("SELECT count(*) FROM sessions") == 0


async def test_a_session_cannot_reference_a_user_that_is_not_there(prepared):
    async with prepared.acquire() as connection:
        with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
            await connection.execute(
                INSERT_SESSION,
                uuid4(),
                hash_session_token(generate_session_token()),
            )

    assert raised.value.constraint_name == "sessions_user_fk"


# --- the service, end to end -------------------------------------------


async def test_registering_then_signing_in_identifies_the_same_person(service):
    registered = await service.register(email=EMAIL, password=PASSWORD, name=NAME)
    signed_in = await service.log_in(email=EMAIL, password=PASSWORD)

    assert signed_in.user.id == registered.user.id
    assert signed_in.user.email == EMAIL
    assert signed_in.user.name == NAME

    # Two log-ins, two sessions: the second must not reissue the first.
    assert signed_in.issued.token != registered.issued.token

    viewer = await service.authenticate(signed_in.issued.token)

    assert viewer is not None
    assert viewer.id == registered.user.id


async def test_the_address_is_matched_regardless_of_case(service):
    await service.register(email=EMAIL, password=PASSWORD, name=None)

    signed_in = await service.log_in(email="ADA@Example.COM", password=PASSWORD)

    assert signed_in.user.email == EMAIL


async def test_registering_a_taken_address_is_a_validation_error(service):
    """The unique constraint, translated into something a form can render.

    This is the path that has no equivalent in the fake tests: the refusal
    comes from PostgreSQL, is caught by constraint name in the repository,
    and becomes a ValidationError in the service. Every layer is real.
    """
    await service.register(email=EMAIL, password=PASSWORD, name=None)

    with pytest.raises(ValidationError) as raised:
        await service.register(email=EMAIL, password="another password", name=None)

    assert [(issue.field, issue.code) for issue in raised.value.issues] == [
        ("email", "EMAIL_TAKEN")
    ]


async def test_a_wrong_password_and_an_unknown_address_fail_identically(service):
    await service.register(email=EMAIL, password=PASSWORD, name=None)

    with pytest.raises(AuthenticationError) as wrong:
        await service.log_in(email=EMAIL, password="not the password")

    with pytest.raises(AuthenticationError) as unknown:
        await service.log_in(email="nobody@example.com", password=PASSWORD)

    assert type(wrong.value) is type(unknown.value)
    assert wrong.value.args == unknown.value.args


async def test_the_stored_password_hash_verifies_and_is_not_the_password(
    prepared, service
):
    await service.register(email=EMAIL, password=PASSWORD, name=NAME)

    stored = await rows_as_text(prepared, USERS_AS_TEXT)

    assert "$argon2id$" in stored
    assert PASSWORD not in stored

    # And not a single word of it, which is what would survive a "hash" that
    # was really an encoding.
    for word in PASSWORD.split():
        assert word not in stored


async def test_the_raw_token_is_nowhere_in_the_sessions_table(prepared, service):
    """The claim the whole digest design rests on.

    Asked of the rendered row rather than of a named column, so that a
    column added later is covered by this test on the day it is added.
    """
    registered = await service.register(email=EMAIL, password=PASSWORD, name=NAME)
    token = registered.issued.token

    stored = await rows_as_text(prepared, SESSIONS_AS_TEXT)

    assert token not in stored
    assert hash_session_token(token).hex() in stored.replace("\\x", "")


# --- sessions, over a server that owns the clock -----------------------


async def test_an_expired_session_identifies_nobody(prepared, service):
    """Expiry is enforced by the query, not by a comparison in Python.

    Written through the repository with a negative lifetime, so the row is
    expired by the same arithmetic that issues a live one -- `now()` plus an
    interval, on the database's clock.
    """
    registered = await service.register(email=EMAIL, password=PASSWORD, name=NAME)
    token = generate_session_token()

    async with prepared.acquire() as connection:
        await SessionRepository().create(
            connection,
            user_id=registered.user.id,
            token_hash=hash_session_token(token),
            lifetime=timedelta(seconds=-1),
        )

    assert await service.authenticate(token) is None

    # And the live session issued at registration still works, so the test
    # above is about expiry and not about the session being unfindable.
    assert await service.authenticate(registered.issued.token) is not None


async def test_a_session_is_stamped_when_it_is_used(prepared, service):
    """`last_used_at`, which is the only thing making the column worth having.

    NULL until the session is first presented, then set. It is what an idle
    timeout and a "your devices" screen would both be built from.
    """
    registered = await service.register(email=EMAIL, password=PASSWORD, name=NAME)

    async with prepared.acquire() as connection:
        before = await connection.fetchval("SELECT last_used_at FROM sessions")

    assert before is None

    await service.authenticate(registered.issued.token)

    async with prepared.acquire() as connection:
        after = await connection.fetchval("SELECT last_used_at FROM sessions")

    assert after is not None


async def test_logging_out_revokes_the_session_for_good(prepared, service):
    registered = await service.register(email=EMAIL, password=PASSWORD, name=NAME)
    token = registered.issued.token

    assert await service.authenticate(token) is not None

    await service.log_out(token)

    assert await service.authenticate(token) is None

    async with prepared.acquire() as connection:
        remaining = await connection.fetchval("SELECT count(*) FROM sessions")

    # Deleted, not merely expired: nothing sweeps this table yet, so a
    # revoked session that stayed behind would stay forever.
    assert remaining == 0


async def test_logging_out_leaves_other_sessions_alone(service):
    """Signing out of one device must not sign out the rest.

    A log-out that deleted by user id rather than by digest would pass every
    other test in this file.
    """
    first = await service.register(email=EMAIL, password=PASSWORD, name=NAME)
    second = await service.log_in(email=EMAIL, password=PASSWORD)

    await service.log_out(first.issued.token)

    assert await service.authenticate(first.issued.token) is None
    assert await service.authenticate(second.issued.token) is not None


async def test_an_unknown_token_identifies_nobody(service):
    await service.register(email=EMAIL, password=PASSWORD, name=NAME)

    assert await service.authenticate(generate_session_token()) is None
