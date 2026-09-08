"""AuthService against fakes: the rules, and the order things happen in.

No database and no real argon2, so what is asserted here is everything that
is decided in Python -- which for this service is most of what can go wrong.

Three groups, and the middle one is the reason the file is this long.
Validation has to reject bad input before a connection is taken. Failure has
to be indistinguishable between "no such address" and "wrong password", not
only in what it returns but in how much work it does to get there, because
the work is what an attacker can time. And a password or a raw session token
must not reach a repository, a stored value, or anything a caller is handed
back.

The fake hasher records the shape of every call, never the password, and
asserts the pool is idle whenever it runs. That last one is not decoration:
holding a pooled connection across a ~100ms hash is how five concurrent
log-ins take a five-connection pool out of service, and it is invisible in
any test that only checks the return value.
"""

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from inspect import signature
from uuid import UUID, uuid4

import pytest

from app.domain.auth import SessionEntity, UserCredentials, UserEntity
from app.domain.errors import (
    AuthenticationError,
    EmailAlreadyRegisteredError,
    ValidationError,
)
from app.repositories.rate_limits import RateLimitRepository
from app.repositories.sessions import SessionRepository
from app.services.auth import (
    EMAIL_MAX_LENGTH,
    LOGIN_BUDGETS,
    LOGIN_EMAIL_SCOPE,
    LOGIN_IP_SCOPE,
    NAME_MAX_LENGTH,
    PASSWORD_MAX_LENGTH,
    PASSWORD_MIN_LENGTH,
    RATE_LIMIT_WINDOW,
    REGISTER_BUDGETS,
    REGISTER_IP_SCOPE,
    SWEEP_BATCH,
    AuthService,
    _subject_for_email,
    run_sweep_loop,
)
from app.services.passwords import Argon2PasswordHasher
from app.services.tokens import hash_session_token

from tests.conftest import ExplodingPool


EMAIL = "ada@example.com"
PASSWORD = "correct horse battery staple"
NAME = "Ada Lovelace"

BASE_TIME = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def fake_encoded_hash(password: str) -> str:
    """A stand-in for an argon2 hash: same prefix, none of the cost.

    A digest rather than the password itself, so that a test asserting "the
    password does not appear in what was stored" cannot pass by accident on
    a fake that stored it in plain sight.
    """
    digest = hashlib.sha256(password.encode("utf-8")).hexdigest()

    return f"$argon2id$fake${digest}"


class FakeHasher:
    """Records what it was asked to do, and when the pool was busy.

    `operations` holds one entry per call, and deliberately holds no
    password: a test fixture that accumulated plaintext passwords in memory
    would be the same mistake the code under test is being checked for.
    """

    def __init__(self, pool=None):
        self.operations: list[str] = []
        self._pool = pool

    def _check_pool_is_idle(self, operation: str) -> None:
        if self._pool is None:
            return

        assert self._pool.checked_out == 0, (
            f"{operation} ran while holding {self._pool.checked_out} pooled "
            "connection(s); argon2 must not be awaited inside pool.acquire()"
        )

    async def hash(self, password: str) -> str:
        self._check_pool_is_idle("hash")
        self.operations.append("hash")

        return fake_encoded_hash(password)

    async def verify(self, *, password_hash: str, password: str) -> bool:
        self._check_pool_is_idle("verify")
        self.operations.append("verify")

        return password_hash == fake_encoded_hash(password)

    async def verify_decoy(self, password: str) -> None:
        self._check_pool_is_idle("verify_decoy")

        # Recorded as a plain "verify" on purpose. The whole point of the
        # decoy is that it is the same work as a real verification, so a
        # test comparing the two paths must not be able to tell them apart
        # here either.
        self.operations.append("verify")


class FakeConnection:
    """Enough of asyncpg.Connection for a service to open a transaction on."""

    def __init__(self):
        self.transactions = 0

    def transaction(self):
        self.transactions += 1

        return _Transaction()


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class _Acquire:
    def __init__(self, pool):
        self._pool = pool

    async def __aenter__(self):
        self._pool.checked_out += 1

        return self._pool.connection

    async def __aexit__(self, *exc_info):
        self._pool.checked_out -= 1

        return False


class FakePool:
    """Counts acquisitions, and how many are outstanding right now.

    `checked_out` is what makes "the service released the connection before
    hashing" observable at all.
    """

    def __init__(self):
        self.connection = FakeConnection()
        self.acquire_count = 0
        self.checked_out = 0

    def acquire(self):
        self.acquire_count += 1

        return _Acquire(self)


class FakeUserRepository:
    """An in-memory `users` table, keyed the way the real one is.

    `calls` records every argument every method was handed, so that a test
    can assert what did *not* travel into the repository as easily as what
    did.
    """

    def __init__(self, users=None):
        self.by_email: dict[str, tuple[UserEntity, str]] = dict(users or {})
        self.calls: list[tuple] = []

    async def create(self, connection, *, email, password_hash, name):
        self.calls.append(("create", email, password_hash, name))

        if email in self.by_email:
            raise EmailAlreadyRegisteredError()

        user = UserEntity(
            id=uuid4(),
            email=email,
            name=name,
            created_at=BASE_TIME,
            updated_at=BASE_TIME,
        )
        self.by_email[email] = (user, password_hash)

        return user

    async def find_credentials_by_email(self, connection, email):
        self.calls.append(("find_credentials_by_email", email))

        found = self.by_email.get(email)

        if found is None:
            return None

        user, password_hash = found

        return UserCredentials(user_id=user.id, password_hash=password_hash)

    async def get_by_id(self, connection, user_id):
        self.calls.append(("get_by_id", user_id))

        for user, _ in self.by_email.values():
            if user.id == user_id:
                return user

        return None


class FakeSessionRepository:
    """An in-memory `sessions` table that only ever sees digests."""

    def __init__(self):
        self.rows: dict[bytes, SessionEntity] = {}
        self.calls: list[tuple] = []

    async def create(self, connection, *, user_id, token_hash, lifetime):
        self.calls.append(("create", user_id, token_hash, lifetime))

        session = SessionEntity(
            id=uuid4(),
            user_id=user_id,
            created_at=BASE_TIME,
            last_used_at=None,
            expires_at=BASE_TIME + lifetime,
        )
        self.rows[token_hash] = session

        return session

    async def touch_valid(self, connection, token_hash):
        self.calls.append(("touch_valid", token_hash))

        return self.rows.get(token_hash)

    async def delete_by_token_hash(self, connection, token_hash):
        self.calls.append(("delete_by_token_hash", token_hash))

        return self.rows.pop(token_hash, None) is not None

    async def delete_expired(self, connection, *, batch):
        self.calls.append(("delete_expired", batch))

        expired = [
            token_hash
            for token_hash, session in self.rows.items()
            if session.expires_at <= self.now
        ][:batch]

        for token_hash in expired:
            del self.rows[token_hash]

        return len(expired)

    # The instant `delete_expired` compares against, which the real repository
    # takes from `now()` in the server. A fake cannot have a database clock, so
    # a test that wants a session collected moves this rather than sleeping.
    now = BASE_TIME


class FakeRateLimitRepository:
    """An in-memory `auth_rate_limits`, counting the way the real one counts.

    A real counter rather than a stub returning a number, because every
    assertion about the limiter is an assertion about arithmetic across
    several calls -- that the eleventh attempt is refused and the tenth is
    not, that a success puts a subject back at zero -- and a stub that answers
    a fixed count cannot be wrong in any of the ways that matter.

    Deliberately does NOT roll the window. The real rollover is a CASE inside
    one upsert and is a claim about SQL, so it is proved in
    tests/test_migration_032_db.py against a server and nowhere else. A Python
    reimplementation here would be a second window implementation that could
    agree with itself while disagreeing with the one that ships.
    """

    def __init__(self):
        self.counts: dict[tuple[str, str], int] = {}
        self.calls: list[tuple] = []

    async def consume(self, connection, *, buckets, window):
        self.calls.append(("consume", tuple(buckets), window))

        attempts = {}

        for scope, subject in buckets:
            key = (scope, subject)
            self.counts[key] = self.counts.get(key, 0) + 1
            attempts[scope] = self.counts[key]

        return attempts

    async def clear(self, connection, *, buckets):
        self.calls.append(("clear", tuple(buckets)))

        for scope, subject in buckets:
            self.counts.pop((scope, subject), None)

    async def delete_stale(self, connection, *, window, batch):
        self.calls.append(("delete_stale", window, batch))

        # Everything is "stale" here, because this fake holds no timestamps.
        # What the sweep tests need from this method is that it is called, that
        # it is batched, and that the pass loops until a batch comes back
        # short; whether a particular row was old enough is the SQL's claim.
        stale = list(self.counts)[:batch]

        for key in stale:
            del self.counts[key]

        return len(stale)


def argument_shape(method) -> list[tuple[str, object]]:
    """A callable's parameters, by name and by how they may be passed.

    Names and kinds rather than whole signatures, unlike the hasher check
    below, and the difference is what each fake is. FakeHasher is annotated
    exactly like the real hasher, so comparing whole signatures there costs
    nothing and catches more. The repository fakes are not annotated -- the
    rest of this file's fakes are not either -- so a whole-signature
    comparison would fail on `connection: asyncpg.Connection` versus
    `connection`, which is not drift, and would have to be silenced by
    annotating a fake for the benefit of the test that checks it.

    What actually broke here was an ARGUMENT: a service that started passing
    `client_ip=` to fakes that took no such thing. Names and kinds are exactly
    that failure, and keyword-only versus positional is part of it -- a fake
    that accepted `buckets` positionally would answer a call the real
    repository refuses.
    """
    return [(p.name, p.kind) for p in signature(method).parameters.values()]


@pytest.mark.parametrize(
    "method",
    ["consume", "clear", "delete_stale"],
)
def test_the_fake_rate_limiter_takes_the_real_repository_s_arguments(method):
    """The guard that would have caught this file's last breakage.

    Every assertion below about budgets, refusals and the sweep runs against
    FakeRateLimitRepository. If RateLimitRepository renames a keyword or adds
    one, those tests go on passing against a shape the service no longer
    calls -- which is exactly what happened when `client_ip` was added, and is
    why this exists alongside the hasher's version of it.
    """
    assert argument_shape(getattr(FakeRateLimitRepository, method)) == argument_shape(
        getattr(RateLimitRepository, method)
    )


def test_the_fake_session_repository_takes_the_real_one_s_sweep_arguments():
    """The same guard, for the one method the sweep depends on."""
    assert argument_shape(FakeSessionRepository.delete_expired) == argument_shape(
        SessionRepository.delete_expired
    )


@pytest.mark.parametrize("method", ["hash", "verify", "verify_decoy"])
def test_the_fake_hasher_has_the_real_hasher_s_signature(method):
    """A fake that has drifted from the thing it stands in for proves nothing.

    Every assertion in this file about ordering, about the decoy, and about
    what the pool is doing runs against FakeHasher. If Argon2PasswordHasher
    grows an argument, or renames one, or stops being awaitable, those tests
    keep passing against a shape the service no longer calls -- and this is
    the one check that notices.
    """
    assert signature(getattr(FakeHasher, method)) == signature(
        getattr(Argon2PasswordHasher, method)
    )


def build_service(
    pool=None,
    hasher=None,
    users=None,
    sessions=None,
    rate_limits=None,
    **kwargs,
):
    pool = pool if pool is not None else FakePool()
    hasher = hasher if hasher is not None else FakeHasher(pool)

    return AuthService(
        pool=pool,
        users=users if users is not None else FakeUserRepository(),
        sessions=sessions if sessions is not None else FakeSessionRepository(),
        hasher=hasher,
        rate_limits=(
            rate_limits if rate_limits is not None else FakeRateLimitRepository()
        ),
        **kwargs,
    )


@pytest.fixture
def pool() -> FakePool:
    return FakePool()


@pytest.fixture
def users() -> FakeUserRepository:
    return FakeUserRepository()


@pytest.fixture
def sessions() -> FakeSessionRepository:
    return FakeSessionRepository()


@pytest.fixture
def rate_limits() -> FakeRateLimitRepository:
    return FakeRateLimitRepository()


@pytest.fixture
def hasher(pool) -> FakeHasher:
    return FakeHasher(pool)


@pytest.fixture
def service(pool, hasher, users, sessions, rate_limits) -> AuthService:
    return build_service(
        pool=pool,
        hasher=hasher,
        users=users,
        sessions=sessions,
        rate_limits=rate_limits,
    )


def codes(error: ValidationError) -> list[tuple[str, str]]:
    return [(issue.field, issue.code) for issue in error.issues]


# --- registration validation -------------------------------------------


@pytest.mark.parametrize(
    ("email", "password", "name", "expected"),
    [
        ("", PASSWORD, None, [("email", "REQUIRED")]),
        ("a@", PASSWORD, None, [("email", "INVALID")]),
        ("ada", PASSWORD, None, [("email", "INVALID")]),
        ("@example.com", PASSWORD, None, [("email", "INVALID")]),
        ("a@b@c.com", PASSWORD, None, [("email", "INVALID")]),
        # Whitespace is rejected rather than trimmed away; see
        # AuthService._normalized_email.
        (" ada@example.com", PASSWORD, None, [("email", "INVALID")]),
        ("ada@example.com ", PASSWORD, None, [("email", "INVALID")]),
        ("a" * 320 + "@example.com", PASSWORD, None, [("email", "TOO_LONG")]),
        (EMAIL, "short", None, [("password", "TOO_SHORT")]),
        (EMAIL, "", None, [("password", "TOO_SHORT")]),
        (EMAIL, "x" * (PASSWORD_MAX_LENGTH + 1), None, [("password", "TOO_LONG")]),
        (EMAIL, PASSWORD, "n" * (NAME_MAX_LENGTH + 1), [("name", "TOO_LONG")]),
        # Every field wrong at once, reported in one response and in a
        # documented order, so a form can render all three at the same time.
        (
            "nope",
            "x",
            "n" * (NAME_MAX_LENGTH + 1),
            [("email", "INVALID"), ("password", "TOO_SHORT"), ("name", "TOO_LONG")],
        ),
    ],
)
async def test_invalid_registration_is_rejected_with_structured_issues(
    email, password, name, expected
):
    """And rejected before a connection is taken.

    ExplodingPool fails the test if anything acquires one, which is what
    stops validation from drifting into the database layer -- a service that
    let a malformed address reach an INSERT would be relying on a constraint
    to produce an error message no client can read.
    """
    service = build_service(pool=ExplodingPool(), hasher=FakeHasher())

    with pytest.raises(ValidationError) as raised:
        await service.register(
            email=email, password=password, name=name, client_ip=None
        )

    assert codes(raised.value) == expected


async def test_a_password_at_the_floor_is_accepted(service):
    """The boundary, from the allowed side.

    An off-by-one in the length check would reject a password the message
    says is fine, and the message is a public contract.
    """
    await service.register(
        email=EMAIL,
        password="x" * PASSWORD_MIN_LENGTH,
        name=None,
        client_ip=None,
    )


async def test_an_email_at_the_ceiling_is_accepted(service):
    email = "a" * (EMAIL_MAX_LENGTH - len("@example.com")) + "@example.com"

    authentication = await service.register(
        email=email, password=PASSWORD, name=None, client_ip=None
    )

    assert authentication.user.email == email


async def test_registration_hashes_before_it_touches_the_pool(pool, hasher, service):
    """Ordering, asserted through the fake's own pool check.

    The assertion lives in FakeHasher._check_pool_is_idle and fires from
    inside `hash`, so this test fails if a future refactor moves the hashing
    inside the `async with pool.acquire()` block.

    TWO acquisitions now, not one, and the second one is the point rather
    than a regression: the rate limiter counts the attempt in an acquisition
    of its own that is RELEASED before the hash begins. A limiter that held
    its connection across the hash would be the very thing this test exists
    to forbid, and the fake's pool check is what would catch it.
    """
    await service.register(email=EMAIL, password=PASSWORD, name=NAME, client_ip=None)

    assert hasher.operations == ["hash"]
    assert pool.acquire_count == 2
    assert pool.checked_out == 0


# --- registration behaviour --------------------------------------------


async def test_registration_stores_a_hash_and_never_the_password(users, service):
    await service.register(email=EMAIL, password=PASSWORD, name=NAME, client_ip=None)

    (operation, email, password_hash, name) = users.calls[0]

    assert operation == "create"
    assert password_hash != PASSWORD
    assert PASSWORD not in password_hash
    assert password_hash.startswith("$argon2id$")

    # And nothing else handed to the repository is the password either.
    assert PASSWORD not in {email, name}


async def test_registration_folds_the_address_to_lowercase(users, service):
    """Two spellings of one address are one account.

    Folded in the service, not in the repository and not in SQL, so that the
    stored value is the canonical one and `users_email_key` -- a plain
    case-sensitive UNIQUE -- is enough to enforce it.
    """
    authentication = await service.register(
        email="Ada@Example.COM",
        password=PASSWORD,
        name=None,
        client_ip=None,
    )

    assert authentication.user.email == EMAIL
    assert users.calls[0][1] == EMAIL


async def test_an_empty_name_is_stored_as_no_name(users, service):
    await service.register(email=EMAIL, password=PASSWORD, name="", client_ip=None)

    assert users.calls[0][3] is None


async def test_registration_issues_a_session_in_the_same_transaction(
    pool, sessions, service
):
    """A user without a session is an account whose owner was told otherwise.

    ONE transaction: both writes land together or neither does. The claim is
    the transaction count and not the acquire count -- the limiter takes a
    connection of its own before the hash, and it deliberately runs in no
    transaction at all, because an attempt has to stay counted whether or not
    the registration it belongs to commits.
    """
    await service.register(email=EMAIL, password=PASSWORD, name=NAME, client_ip=None)

    assert pool.connection.transactions == 1
    assert [call[0] for call in sessions.calls] == ["create"]


async def test_registration_stores_the_digest_and_returns_the_token_once(
    sessions, service
):
    """The relationship the whole session mechanism rests on.

    What the caller gets is a token; what the table gets is its digest. The
    token must be recoverable from neither.
    """
    authentication = await service.register(
        email=EMAIL, password=PASSWORD, name=NAME, client_ip=None
    )
    token = authentication.issued.token

    (_, _, stored_hash, _) = sessions.calls[0]

    assert stored_hash == hash_session_token(token)
    assert token.encode("utf-8") not in stored_hash
    assert list(sessions.rows) == [hash_session_token(token)]


async def test_a_taken_address_is_a_validation_error_not_a_database_error(service):
    """The repository's refusal becomes something a form can render.

    Registration does disclose that an address is taken -- see
    AuthService.register on why that is accepted here and why log-in must
    not.
    """
    await service.register(email=EMAIL, password=PASSWORD, name=None, client_ip=None)

    with pytest.raises(ValidationError) as raised:
        await service.register(
            email=EMAIL, password="a different one", name=None, client_ip=None
        )

    assert codes(raised.value) == [("email", "EMAIL_TAKEN")]


async def test_a_taken_address_is_detected_after_case_folding(service):
    await service.register(email=EMAIL, password=PASSWORD, name=None, client_ip=None)

    with pytest.raises(ValidationError) as raised:
        await service.register(
            email="ADA@EXAMPLE.COM", password=PASSWORD, name=None, client_ip=None
        )

    assert codes(raised.value) == [("email", "EMAIL_TAKEN")]


# --- log-in: the failures must be one failure --------------------------


async def registered(service) -> None:
    await service.register(email=EMAIL, password=PASSWORD, name=NAME, client_ip=None)


async def test_an_unknown_address_fails(service):
    with pytest.raises(AuthenticationError):
        await service.log_in(
            email="nobody@example.com", password=PASSWORD, client_ip=None
        )


async def test_a_wrong_password_fails(service):
    await registered(service)

    with pytest.raises(AuthenticationError):
        await service.log_in(email=EMAIL, password="not the password", client_ip=None)


async def test_the_two_failures_are_the_same_failure(service):
    """Same type, same message, nothing to tell them apart.

    An exception that carried the reason would eventually be logged with it,
    rendered with it, or matched on it -- and any of those turns the
    distinction into an answer to "does this address have an account here".
    """
    await registered(service)

    with pytest.raises(AuthenticationError) as unknown:
        await service.log_in(
            email="nobody@example.com", password=PASSWORD, client_ip=None
        )

    with pytest.raises(AuthenticationError) as wrong:
        await service.log_in(email=EMAIL, password="not the password", client_ip=None)

    assert type(unknown.value) is type(wrong.value)
    assert str(unknown.value) == str(wrong.value)
    assert unknown.value.args == wrong.value.args


async def test_the_two_failures_cost_the_same_work(hasher, service):
    """The timing half of the same guarantee.

    Wall-clock is not asserted -- a stopwatch on a shared runner measures the
    runner -- but the work is, and the work is what the clock would be
    measuring. Both paths must perform exactly one verification: the
    wrong-password path against the stored hash, the unknown-address path
    against the decoy. A service that skipped the decoy would show up here as
    an empty operations list.
    """
    await registered(service)

    hasher.operations.clear()
    with pytest.raises(AuthenticationError):
        await service.log_in(
            email="nobody@example.com", password=PASSWORD, client_ip=None
        )
    unknown_address = list(hasher.operations)

    hasher.operations.clear()
    with pytest.raises(AuthenticationError):
        await service.log_in(email=EMAIL, password="not the password", client_ip=None)
    wrong_password = list(hasher.operations)

    assert unknown_address == wrong_password == ["verify"]


async def test_an_oversized_submission_is_refused_without_hashing_or_a_query(hasher):
    """The one early return, and what it is allowed to leak.

    It depends only on the size of what was submitted, which the submitter
    already knows, so it discloses nothing about who has an account. Without
    it a client can make the server carry an arbitrarily large string into a
    worker thread.
    """
    service = build_service(pool=ExplodingPool(), hasher=hasher)

    with pytest.raises(AuthenticationError):
        await service.log_in(
            email="a" * (EMAIL_MAX_LENGTH + 1), password=PASSWORD, client_ip=None
        )

    with pytest.raises(AuthenticationError):
        await service.log_in(
            email=EMAIL, password="x" * (PASSWORD_MAX_LENGTH + 1), client_ip=None
        )

    assert hasher.operations == []


# --- log-in: success ---------------------------------------------------


async def test_a_correct_password_issues_a_new_session(sessions, service):
    await registered(service)

    before = set(sessions.rows)

    authentication = await service.log_in(
        email=EMAIL, password=PASSWORD, client_ip=None
    )
    token = authentication.issued.token

    assert authentication.user.email == EMAIL
    assert hash_session_token(token) in set(sessions.rows) - before


async def test_log_in_folds_the_address_to_lowercase(service):
    await registered(service)

    authentication = await service.log_in(
        email="ADA@example.com", password=PASSWORD, client_ip=None
    )

    assert authentication.user.email == EMAIL


async def test_log_in_never_holds_a_connection_while_verifying(pool, service):
    """The pool-starvation property, asserted from inside the hasher.

    Five concurrent log-ins holding the five-connection pool for the ~100ms
    an argon2 verification takes would stall every unrelated query in the
    process. FakeHasher fails the test if a connection is checked out when
    it runs.
    """
    await registered(service)

    await service.log_in(email=EMAIL, password=PASSWORD, client_ip=None)

    assert pool.checked_out == 0


async def test_the_session_lifetime_reaches_the_repository(sessions):
    """Policy is the service's, arithmetic is the database's.

    The repository is handed a duration and computes the expiry from the
    server clock; nothing here computes a timestamp in Python.
    """
    lifetime = timedelta(minutes=7)
    service = build_service(sessions=sessions, session_lifetime=lifetime)

    await service.register(email=EMAIL, password=PASSWORD, name=None, client_ip=None)

    assert sessions.calls[0][3] == lifetime
    assert service.session_lifetime == lifetime


# --- sessions ----------------------------------------------------------


async def test_a_valid_token_identifies_its_user(service):
    authentication = await service.register(
        email=EMAIL,
        password=PASSWORD,
        name=NAME,
        client_ip=None,
    )

    found = await service.authenticate(authentication.issued.token)

    assert found is not None
    assert found.id == authentication.user.id


async def test_the_session_is_looked_up_by_digest_and_never_by_token(sessions, service):
    authentication = await service.register(
        email=EMAIL,
        password=PASSWORD,
        name=NAME,
        client_ip=None,
    )
    token = authentication.issued.token

    sessions.calls.clear()
    await service.authenticate(token)

    assert sessions.calls[0] == ("touch_valid", hash_session_token(token))


async def test_an_unknown_token_identifies_nobody(service):
    assert await service.authenticate("not a real token") is None


async def test_no_token_identifies_nobody_and_asks_nothing():
    """An anonymous request must not cost a database round trip.

    Every unauthenticated visitor sends one, including the introspection
    GraphiQL fires on page load.
    """
    service = build_service(pool=ExplodingPool())

    assert await service.authenticate(None) is None


async def test_a_session_whose_user_is_gone_identifies_nobody(users, service):
    """The row survives the fake's cascade; the answer must still be None.

    Against PostgreSQL the FK removes it, so this is the belt to that
    braces: a caller must not receive a half-answer if the two ever
    disagree.
    """
    authentication = await service.register(
        email=EMAIL,
        password=PASSWORD,
        name=NAME,
        client_ip=None,
    )

    users.by_email.clear()

    assert await service.authenticate(authentication.issued.token) is None


async def test_logging_out_deletes_the_session_by_digest(sessions, service):
    authentication = await service.register(
        email=EMAIL,
        password=PASSWORD,
        name=NAME,
        client_ip=None,
    )
    token = authentication.issued.token

    await service.log_out(token)

    assert sessions.rows == {}
    assert ("delete_by_token_hash", hash_session_token(token)) in sessions.calls
    assert await service.authenticate(token) is None


async def test_logging_out_twice_is_not_an_error(service):
    """Nor is logging out with a token that never existed.

    Reporting the difference would confirm to whoever holds a token whether
    it was ever real.
    """
    authentication = await service.register(
        email=EMAIL,
        password=PASSWORD,
        name=NAME,
        client_ip=None,
    )

    await service.log_out(authentication.issued.token)
    await service.log_out(authentication.issued.token)
    await service.log_out("a token nobody ever issued")


async def test_logging_out_without_a_session_asks_nothing():
    service = build_service(pool=ExplodingPool())

    await service.log_out(None)


async def test_a_session_is_not_shared_between_log_ins(sessions, service):
    """Each log-in mints its own token; signing in twice does not reissue one.

    A service that returned the existing session would make "sign out on
    this device" sign out every device, and would hand the same secret to
    two machines.
    """
    await registered(service)

    first = await service.log_in(email=EMAIL, password=PASSWORD, client_ip=None)
    second = await service.log_in(email=EMAIL, password=PASSWORD, client_ip=None)

    assert first.issued.token != second.issued.token
    assert len(sessions.rows) == 3  # one from register, two from the log-ins


# --- what never leaves the service -------------------------------------


def flatten(calls) -> list[str]:
    """Every argument of every recorded call, as a string.

    UUIDs and timedeltas included, because the check below is a search for a
    secret and a secret can hide in any of them.
    """
    found: list[str] = []

    for call in calls:
        for argument in call:
            if isinstance(argument, bytes):
                found.append(argument.hex())
            elif isinstance(argument, UUID | timedelta):
                found.append(str(argument))
            else:
                found.append(str(argument))

    return found


async def test_no_repository_call_ever_carries_the_password(users, sessions, service):
    await service.register(email=EMAIL, password=PASSWORD, name=NAME, client_ip=None)
    await service.log_in(email=EMAIL, password=PASSWORD, client_ip=None)

    with pytest.raises(AuthenticationError):
        await service.log_in(email=EMAIL, password="wrong", client_ip=None)

    for value in flatten(users.calls) + flatten(sessions.calls):
        assert PASSWORD not in value


async def test_no_repository_call_ever_carries_the_raw_token(users, sessions, service):
    authentication = await service.register(
        email=EMAIL,
        password=PASSWORD,
        name=NAME,
        client_ip=None,
    )
    token = authentication.issued.token

    await service.authenticate(token)
    await service.log_out(token)

    for value in flatten(users.calls) + flatten(sessions.calls):
        assert token not in value


# --- rate limiting ------------------------------------------------------
#
# The budgets are policy and live in app/services/auth.py; every test below
# reads them from there rather than restating a number, so that changing a
# budget is one edit and not a hunt for the tests that hardcoded it.
#
# What is asserted here is the part decided in Python: which buckets an
# attempt spends, when a refusal happens, what the refusal costs, and what it
# says. The counting itself -- the upsert, the window rollover, the atomicity
# under two replicas -- is SQL, and is proved against a server in
# tests/test_migration_032_db.py.

CLIENT_IP = "203.0.113.7"

LOGIN_EMAIL_BUDGET = LOGIN_BUDGETS[LOGIN_EMAIL_SCOPE]
LOGIN_IP_BUDGET = LOGIN_BUDGETS[LOGIN_IP_SCOPE]
REGISTER_IP_BUDGET = REGISTER_BUDGETS[REGISTER_IP_SCOPE]


async def fail_log_in(service, *, email=EMAIL, password="wrong", client_ip=None):
    """One failed attempt, asserting only that it failed.

    Every log-in in this section is expected to raise; the interesting part is
    always what it cost or what it left behind, never that it raised.
    """
    with pytest.raises(AuthenticationError):
        await service.log_in(email=email, password=password, client_ip=client_ip)


async def register_user(service, *, email=EMAIL, client_ip=None):
    return await service.register(
        email=email,
        password=PASSWORD,
        name=NAME,
        client_ip=client_ip,
    )


async def test_the_address_budget_permits_its_last_attempt_and_refuses_the_next(
    hasher, service
):
    """The boundary, from both sides.

    Off by one here is a limit quietly one tighter -- or one looser -- than the
    number written above it in the policy block, and neither direction shows up
    in a test that only checks that a refusal eventually happens.

    The refusal is told apart from an ordinary failure by its COST, not by its
    error, because the two errors are deliberately identical. A refused attempt
    verifies nothing.
    """
    for _ in range(LOGIN_EMAIL_BUDGET):
        await fail_log_in(service)

    assert len(hasher.operations) == LOGIN_EMAIL_BUDGET

    await fail_log_in(service)

    assert len(hasher.operations) == LOGIN_EMAIL_BUDGET


async def test_a_refused_attempt_is_the_same_error_as_a_wrong_password(service):
    """The refusal must not become the enumeration channel.

    A distinguishable "you are rate limited" would vary with something other
    than the caller's own submission -- it would tell whoever spent one attempt
    on an address that somebody has recently been failing log-ins against it,
    which is a signal about a real account and exactly the class of signal the
    decoy hash spends 100ms per request to erase.

    AuthenticationError carries nothing, so "the same error" is the whole
    externally visible answer. The correct password is used for the last
    attempt deliberately: even the right answer is refused, and refused
    identically.
    """
    await register_user(service)

    for _ in range(LOGIN_EMAIL_BUDGET):
        await fail_log_in(service)

    with pytest.raises(AuthenticationError) as refused:
        await service.log_in(email=EMAIL, password=PASSWORD, client_ip=None)

    assert type(refused.value) is AuthenticationError
    assert refused.value.args == ("Authentication failed",)


async def test_a_refused_attempt_neither_hashes_nor_reads_users(hasher, users, service):
    """The amplifier fix, asserted as the absence of both halves of the cost.

    THIS IS THE TEST THAT FAILS IF THE LIMITER IS REVERTED. An unauthenticated
    caller was able to make the server spend a full argon2id operation --
    64 MiB and ~100ms -- per request, for an address that matches nothing,
    because `verify_decoy` correctly spends what a real verification spends.
    Reverting the limiter puts a hash back on this path.

    The users read matters too, and separately: it is the one remaining
    statement whose cost depends on whether the address exists, so a refusal
    that still performed it would leave a measurable difference on the path
    that skips everything else.
    """
    for _ in range(LOGIN_EMAIL_BUDGET):
        await fail_log_in(service)

    hasher.operations.clear()
    users.calls.clear()

    await fail_log_in(service)

    assert hasher.operations == []
    assert users.calls == []


async def test_a_refusal_costs_the_same_for_a_real_address_as_for_an_unknown_one():
    """Two services, one address, one observable answer.

    The decoy makes an ORDINARY failure cost the same whether or not the
    address has an account. This asserts the property survives the shortcut: a
    refused attempt must not become the place where "this address exists" is
    measurable again, which it would be the moment the refusal happened after
    the credential lookup instead of before it.
    """
    costs = []

    for exists in (True, False):
        pool = FakePool()
        hasher = FakeHasher(pool)
        users = FakeUserRepository()
        service = build_service(pool=pool, hasher=hasher, users=users)

        if exists:
            await register_user(service)

        for _ in range(LOGIN_EMAIL_BUDGET):
            await fail_log_in(service)

        hasher.operations.clear()
        users.calls.clear()

        await fail_log_in(service)

        costs.append((list(hasher.operations), [call[0] for call in users.calls]))

    assert costs[0] == costs[1] == ([], [])


async def test_a_successful_log_in_clears_the_budget_it_spent(rate_limits, service):
    """The mitigation that keeps the address bucket from being a lockout.

    A user who fumbles their password most of the way through the budget and
    then gets it right must be back at zero, not one fumble from the edge. It
    is also what stops a shared machine's accumulated typos from eventually
    locking somebody out of an account nobody is attacking.
    """
    await register_user(service)

    for _ in range(LOGIN_EMAIL_BUDGET - 1):
        await fail_log_in(service)

    assert rate_limits.counts

    await service.log_in(email=EMAIL, password=PASSWORD, client_ip=CLIENT_IP)

    assert rate_limits.counts == {}


async def test_a_failed_log_in_does_not_clear_the_budget_it_spent(rate_limits, service):
    """The other half, and the one a refactor is likelier to break.

    Clearing on the way out of any attempt -- in a `finally`, say -- would be a
    limiter that counts to one forever.
    """
    await register_user(service)

    rate_limits.calls.clear()

    await fail_log_in(service, client_ip=CLIENT_IP)

    assert [call[0] for call in rate_limits.calls] == ["consume"]
    assert set(rate_limits.counts) == {
        (LOGIN_EMAIL_SCOPE, _subject_for_email(EMAIL)),
        (LOGIN_IP_SCOPE, CLIENT_IP),
    }


async def test_the_address_bucket_is_a_digest_and_the_address_never_travels(
    rate_limits, service
):
    """No raw address reaches `auth_rate_limits`, on any path.

    Storing them would build a second copy of the user list -- plus every
    address anyone ever guessed at -- in a table with no tenant, no foreign
    key and no reason for anybody to have thought about who may read it. The
    assertion is over every argument of every call rather than over the key
    this test happens to know about, so a future bucket that carried an
    address fails here too.
    """
    await fail_log_in(service, client_ip=CLIENT_IP)

    assert (LOGIN_EMAIL_SCOPE, _subject_for_email(EMAIL)) in rate_limits.counts

    for value in flatten(rate_limits.calls):
        assert EMAIL not in value


async def test_two_spellings_of_one_address_share_one_budget(rate_limits, service):
    """Case folding happens BEFORE the digest, or it may as well not happen.

    `users_email_key` treats the two as one account, so a limiter that gave
    them separate budgets would hand anyone who noticed a free doubling
    against every account on the deployment.
    """
    await fail_log_in(service, email="ADA@Example.COM")
    await fail_log_in(service, email=EMAIL)

    assert rate_limits.counts == {(LOGIN_EMAIL_SCOPE, _subject_for_email(EMAIL)): 2}


async def test_an_attempt_with_no_address_to_key_on_still_has_a_budget(
    rate_limits, service
):
    """A missing IP degrades the limit; it does not disable it.

    `app/http_client_ip.py` is explicit that it cannot always produce an
    address, so a limiter that refused to count without one would be a login
    path whose protection depends on a proxy configuration this repository
    does not own.
    """
    await fail_log_in(service, client_ip=None)

    assert list(rate_limits.counts) == [(LOGIN_EMAIL_SCOPE, _subject_for_email(EMAIL))]


async def test_the_ip_budget_catches_a_spray_across_many_addresses(hasher, service):
    """What the address bucket cannot see, and the reason the IP bucket exists.

    Credential stuffing tries one password against thousands of DIFFERENT
    accounts, so no single address bucket ever reaches two. Only a key the
    attacker holds constant bounds it, and from one host that key is the
    address they are calling from.
    """
    for index in range(LOGIN_IP_BUDGET):
        await fail_log_in(
            service,
            email=f"user{index}@example.com",
            client_ip=CLIENT_IP,
        )

    assert len(hasher.operations) == LOGIN_IP_BUDGET

    await fail_log_in(service, email="one-more@example.com", client_ip=CLIENT_IP)

    assert len(hasher.operations) == LOGIN_IP_BUDGET


async def test_a_spray_from_one_host_does_not_lock_out_a_different_host(
    hasher, service
):
    """The IP bucket must bound its own caller and nobody else's.

    An IP budget that leaked across addresses would be one attacker denying
    the product to everybody, which is a worse outage than the one it prevents.
    """
    for index in range(LOGIN_IP_BUDGET + 1):
        await fail_log_in(
            service,
            email=f"user{index}@example.com",
            client_ip=CLIENT_IP,
        )

    before = len(hasher.operations)

    await fail_log_in(service, email="elsewhere@example.com", client_ip="198.51.100.4")

    assert len(hasher.operations) == before + 1


async def test_registration_is_refused_after_its_budget_and_is_allowed_to_say_so(
    hasher, service
):
    """The one refusal in this service that names itself.

    Registration already discloses that an address is taken -- that is the
    channel this budget slows, not a secret a message could spoil -- and the
    bucket is keyed on the caller's own address and nothing else, so the answer
    depends only on what the caller has themselves recently done. There is
    nothing here to learn about anybody else, and somebody genuinely signing up
    deserves to be told to wait.

    It must also refuse BEFORE the hash, for the same reason log-in does.
    """
    for index in range(REGISTER_IP_BUDGET):
        await register_user(
            service,
            email=f"user{index}@example.com",
            client_ip=CLIENT_IP,
        )

    hasher.operations.clear()

    with pytest.raises(ValidationError) as refused:
        await register_user(service, email="one-more@example.com", client_ip=CLIENT_IP)

    assert codes(refused.value) == [("email", "TOO_MANY_ATTEMPTS")]
    assert hasher.operations == []


async def test_a_malformed_registration_spends_no_budget(rate_limits, service):
    """A typo must not cost somebody a share of their own budget.

    Validation runs first and touches neither the database nor the hasher, so
    refusing malformed input is free and does not need to be counted.
    """
    with pytest.raises(ValidationError):
        await service.register(
            email="nope",
            password="short",
            name=None,
            client_ip=CLIENT_IP,
        )

    assert rate_limits.calls == []


async def test_registration_without_an_address_has_no_budget(rate_limits, service):
    """The accepted consequence, asserted rather than left as prose.

    Enumeration walks a different address every time, so the submitted email is
    a key that never repeats and would never trip -- the caller's IP is the
    only key that sees the sweep at all. A deployment that cannot produce one
    therefore has no registration budget, which is why `read_client_ip` falls
    back to the TCP peer rather than giving up. This test exists so that the
    day somebody adds a second register bucket, it fails and gets read.
    """
    await register_user(service)

    assert rate_limits.calls == [("consume", (), RATE_LIMIT_WINDOW)]


# --- the sweep ----------------------------------------------------------


async def test_the_sweep_collects_expired_sessions_and_reports_the_count(
    sessions, service
):
    """One pass, and the number the log line is built from."""
    await register_user(service)

    sessions.now = BASE_TIME + timedelta(days=365)

    collected_sessions, collected_limits = await service.sweep_once()

    assert (collected_sessions, collected_limits) == (1, 0)
    assert sessions.rows == {}


async def test_the_sweep_leaves_a_live_session_alone(sessions, service):
    """The pass reclaims space; it must not sign anybody out.

    A sweep whose predicate drifted -- `<` for `<=`, a cutoff computed the
    wrong side of the lifetime -- would be indistinguishable from a product
    that randomly logs people out, and would show up as a support ticket
    rather than as a failure.
    """
    authentication = await register_user(service)

    collected_sessions, _ = await service.sweep_once()

    assert collected_sessions == 0
    assert await service.authenticate(authentication.issued.token) is not None


async def test_the_sweep_keeps_going_until_a_batch_comes_back_short(sessions, service):
    """A backlog larger than one batch is drained, not sampled.

    The batch is a bound on each TRANSACTION, not on what a pass collects. A
    sweep that stopped after one statement would fall permanently behind on
    exactly the table that got big enough to need it.
    """
    for index in range(SWEEP_BATCH + 3):
        sessions.rows[index.to_bytes(8, "big")] = SessionEntity(
            id=uuid4(),
            user_id=uuid4(),
            created_at=BASE_TIME,
            last_used_at=None,
            expires_at=BASE_TIME - timedelta(days=1),
        )

    collected_sessions, _ = await service.sweep_once()

    assert collected_sessions == SWEEP_BATCH + 3
    assert sessions.rows == {}
    assert [call for call in sessions.calls if call[0] == "delete_expired"] == [
        ("delete_expired", SWEEP_BATCH),
        ("delete_expired", SWEEP_BATCH),
    ]


async def test_the_sweep_prunes_aged_out_rate_limit_buckets(rate_limits, service):
    """The second table on the same pass, which is what the index is for."""
    await fail_log_in(service, client_ip=CLIENT_IP)

    assert len(rate_limits.counts) == 2

    _, collected_limits = await service.sweep_once()

    assert collected_limits == 2
    assert rate_limits.counts == {}


async def test_the_sweep_releases_its_connection_between_batches(pool, service):
    """A catch-up pass must not hold a fifth of the pool for its duration.

    Acquiring per batch is what keeps a first run against a never-swept table
    invisible to concurrent requests rather than merely bounded.
    """
    await service.sweep_once()

    assert pool.checked_out == 0
    assert pool.acquire_count == 2


async def test_the_sweep_loop_survives_a_failing_pass_and_stops_when_cancelled():
    """A supervisor loop has no supervisor above it.

    Propagating would end the task with nothing to restart it, and both tables
    would silently resume growing forever for the life of the process. The
    cancellation must still get through, or `app.main.lifespan` hangs on
    shutdown waiting for a task that logged its own cancellation and went round
    again.
    """
    passes = []
    recovered = asyncio.Event()

    class Failing:
        async def sweep_once(self):
            passes.append(len(passes))

            if len(passes) == 1:
                raise RuntimeError("the database is gone")

            if len(passes) >= 3:
                recovered.set()

            return 0, 0

    task = asyncio.create_task(run_sweep_loop(Failing(), interval=0))

    # Waited on with a deadline rather than forever: a loop that died on the
    # first failing pass never sets this, and the failure this test exists to
    # catch must be a failure rather than a suite that hangs.
    await asyncio.wait_for(recovered.wait(), timeout=5)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
