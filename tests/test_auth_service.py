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
from app.services.auth import (
    EMAIL_MAX_LENGTH,
    NAME_MAX_LENGTH,
    PASSWORD_MAX_LENGTH,
    PASSWORD_MIN_LENGTH,
    AuthService,
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


def build_service(pool=None, hasher=None, users=None, sessions=None, **kwargs):
    pool = pool if pool is not None else FakePool()
    hasher = hasher if hasher is not None else FakeHasher(pool)

    return AuthService(
        pool=pool,
        users=users if users is not None else FakeUserRepository(),
        sessions=sessions if sessions is not None else FakeSessionRepository(),
        hasher=hasher,
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
def hasher(pool) -> FakeHasher:
    return FakeHasher(pool)


@pytest.fixture
def service(pool, hasher, users, sessions) -> AuthService:
    return build_service(pool=pool, hasher=hasher, users=users, sessions=sessions)


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
        await service.register(email=email, password=password, name=name)

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
    )


async def test_an_email_at_the_ceiling_is_accepted(service):
    email = "a" * (EMAIL_MAX_LENGTH - len("@example.com")) + "@example.com"

    authentication = await service.register(email=email, password=PASSWORD, name=None)

    assert authentication.user.email == email


async def test_registration_hashes_before_it_touches_the_pool(pool, hasher, service):
    """Ordering, asserted through the fake's own pool check.

    The assertion lives in FakeHasher._check_pool_is_idle and fires from
    inside `hash`, so this test fails if a future refactor moves the hashing
    inside the `async with pool.acquire()` block.
    """
    await service.register(email=EMAIL, password=PASSWORD, name=NAME)

    assert hasher.operations == ["hash"]
    assert pool.acquire_count == 1


# --- registration behaviour --------------------------------------------


async def test_registration_stores_a_hash_and_never_the_password(users, service):
    await service.register(email=EMAIL, password=PASSWORD, name=NAME)

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
    )

    assert authentication.user.email == EMAIL
    assert users.calls[0][1] == EMAIL


async def test_an_empty_name_is_stored_as_no_name(users, service):
    await service.register(email=EMAIL, password=PASSWORD, name="")

    assert users.calls[0][3] is None


async def test_registration_issues_a_session_in_the_same_transaction(
    pool, sessions, service
):
    """A user without a session is an account whose owner was told otherwise.

    One acquire and one transaction: both writes land together or neither
    does.
    """
    await service.register(email=EMAIL, password=PASSWORD, name=NAME)

    assert pool.acquire_count == 1
    assert pool.connection.transactions == 1
    assert [call[0] for call in sessions.calls] == ["create"]


async def test_registration_stores_the_digest_and_returns_the_token_once(
    sessions, service
):
    """The relationship the whole session mechanism rests on.

    What the caller gets is a token; what the table gets is its digest. The
    token must be recoverable from neither.
    """
    authentication = await service.register(email=EMAIL, password=PASSWORD, name=NAME)
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
    await service.register(email=EMAIL, password=PASSWORD, name=None)

    with pytest.raises(ValidationError) as raised:
        await service.register(email=EMAIL, password="a different one", name=None)

    assert codes(raised.value) == [("email", "EMAIL_TAKEN")]


async def test_a_taken_address_is_detected_after_case_folding(service):
    await service.register(email=EMAIL, password=PASSWORD, name=None)

    with pytest.raises(ValidationError) as raised:
        await service.register(email="ADA@EXAMPLE.COM", password=PASSWORD, name=None)

    assert codes(raised.value) == [("email", "EMAIL_TAKEN")]


# --- log-in: the failures must be one failure --------------------------


async def registered(service) -> None:
    await service.register(email=EMAIL, password=PASSWORD, name=NAME)


async def test_an_unknown_address_fails(service):
    with pytest.raises(AuthenticationError):
        await service.log_in(email="nobody@example.com", password=PASSWORD)


async def test_a_wrong_password_fails(service):
    await registered(service)

    with pytest.raises(AuthenticationError):
        await service.log_in(email=EMAIL, password="not the password")


async def test_the_two_failures_are_the_same_failure(service):
    """Same type, same message, nothing to tell them apart.

    An exception that carried the reason would eventually be logged with it,
    rendered with it, or matched on it -- and any of those turns the
    distinction into an answer to "does this address have an account here".
    """
    await registered(service)

    with pytest.raises(AuthenticationError) as unknown:
        await service.log_in(email="nobody@example.com", password=PASSWORD)

    with pytest.raises(AuthenticationError) as wrong:
        await service.log_in(email=EMAIL, password="not the password")

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
        await service.log_in(email="nobody@example.com", password=PASSWORD)
    unknown_address = list(hasher.operations)

    hasher.operations.clear()
    with pytest.raises(AuthenticationError):
        await service.log_in(email=EMAIL, password="not the password")
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
        await service.log_in(email="a" * (EMAIL_MAX_LENGTH + 1), password=PASSWORD)

    with pytest.raises(AuthenticationError):
        await service.log_in(email=EMAIL, password="x" * (PASSWORD_MAX_LENGTH + 1))

    assert hasher.operations == []


# --- log-in: success ---------------------------------------------------


async def test_a_correct_password_issues_a_new_session(sessions, service):
    await registered(service)

    before = set(sessions.rows)

    authentication = await service.log_in(email=EMAIL, password=PASSWORD)
    token = authentication.issued.token

    assert authentication.user.email == EMAIL
    assert hash_session_token(token) in set(sessions.rows) - before


async def test_log_in_folds_the_address_to_lowercase(service):
    await registered(service)

    authentication = await service.log_in(email="ADA@example.com", password=PASSWORD)

    assert authentication.user.email == EMAIL


async def test_log_in_never_holds_a_connection_while_verifying(pool, service):
    """The pool-starvation property, asserted from inside the hasher.

    Five concurrent log-ins holding the five-connection pool for the ~100ms
    an argon2 verification takes would stall every unrelated query in the
    process. FakeHasher fails the test if a connection is checked out when
    it runs.
    """
    await registered(service)

    await service.log_in(email=EMAIL, password=PASSWORD)

    assert pool.checked_out == 0


async def test_the_session_lifetime_reaches_the_repository(sessions):
    """Policy is the service's, arithmetic is the database's.

    The repository is handed a duration and computes the expiry from the
    server clock; nothing here computes a timestamp in Python.
    """
    lifetime = timedelta(minutes=7)
    service = build_service(sessions=sessions, session_lifetime=lifetime)

    await service.register(email=EMAIL, password=PASSWORD, name=None)

    assert sessions.calls[0][3] == lifetime
    assert service.session_lifetime == lifetime


# --- sessions ----------------------------------------------------------


async def test_a_valid_token_identifies_its_user(service):
    authentication = await service.register(
        email=EMAIL,
        password=PASSWORD,
        name=NAME,
    )

    found = await service.authenticate(authentication.issued.token)

    assert found is not None
    assert found.id == authentication.user.id


async def test_the_session_is_looked_up_by_digest_and_never_by_token(sessions, service):
    authentication = await service.register(
        email=EMAIL,
        password=PASSWORD,
        name=NAME,
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
    )

    users.by_email.clear()

    assert await service.authenticate(authentication.issued.token) is None


async def test_logging_out_deletes_the_session_by_digest(sessions, service):
    authentication = await service.register(
        email=EMAIL,
        password=PASSWORD,
        name=NAME,
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

    first = await service.log_in(email=EMAIL, password=PASSWORD)
    second = await service.log_in(email=EMAIL, password=PASSWORD)

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
    await service.register(email=EMAIL, password=PASSWORD, name=NAME)
    await service.log_in(email=EMAIL, password=PASSWORD)

    with pytest.raises(AuthenticationError):
        await service.log_in(email=EMAIL, password="wrong")

    for value in flatten(users.calls) + flatten(sessions.calls):
        assert PASSWORD not in value


async def test_no_repository_call_ever_carries_the_raw_token(users, sessions, service):
    authentication = await service.register(
        email=EMAIL,
        password=PASSWORD,
        name=NAME,
    )
    token = authentication.issued.token

    await service.authenticate(token)
    await service.log_out(token)

    for value in flatten(users.calls) + flatten(sessions.calls):
        assert token not in value
