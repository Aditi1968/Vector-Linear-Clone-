import re
from datetime import timedelta
from uuid import UUID

import asyncpg

from app.domain.auth import (
    Authentication,
    IssuedSession,
    UserEntity,
)
from app.domain.errors import (
    AuthenticationError,
    EmailAlreadyRegisteredError,
    ValidationError,
    ValidationIssue,
)
from app.repositories.sessions import SessionRepository
from app.repositories.users import UserRepository
from app.services.passwords import PasswordHasher
from app.services.tokens import generate_session_token, hash_session_token


# RFC 5321's ceiling: 64 octets of local part, '@', 255 of domain. Mirrored
# by users_email_length, which is the backstop and not the check a client
# sees -- a constraint violation produces an internal error, not a message a
# form can render.
#
# There is no matching floor here. EMAIL_PATTERN already refuses anything
# shorter than "a@b", and a separate minimum would only decide which of two
# messages a two-character address gets.
EMAIL_MAX_LENGTH = 320

# Exactly one '@', something either side of it, no whitespace anywhere. The
# same expression as users_email_shape, and as deliberately permissive: an
# address is deliverable or it is not, and no regular expression decides
# that. Rejecting more than this rejects real addresses -- '+' tags, unicode
# domains, single-letter hosts -- and buys nothing, since the only proof an
# address exists is sending something to it.
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+$")

# NIST SP 800-63B: an 8-character floor, and no composition rules. Rules
# about digits and symbols push people toward "Password1!" and buy less than
# the length does.
PASSWORD_MIN_LENGTH = 8

# Not a security limit -- argon2's cost does not grow with input length --
# but a memory one. Without a ceiling a client can post a megabyte and have
# the server carry it into a worker thread; 1024 characters is far past any
# passphrase and far short of a problem.
PASSWORD_MAX_LENGTH = 1024

NAME_MAX_LENGTH = 200

# How long a session is good for. Absolute, not idle-based: a session ends
# fourteen days after it was issued whether or not it was used, which is a
# rule that can be read off the row. `last_used_at` is recorded so that an
# idle bound can be added later without a migration.
SESSION_LIFETIME = timedelta(days=14)


class AuthService:
    """Registration, log-in, log-out, and what a session token is worth.

    Everything this service establishes is a user id. It says nothing about
    which workspaces that user may act in -- membership is a different table,
    a different service, and a question that has to be asked separately for
    every workspace-scoped operation. A caller that treats "authenticated"
    as "allowed" has skipped the check, and there is nothing here that would
    stop them, so it is worth stating: holding a UserEntity from this service
    grants access to nothing.

    The service owns connection acquisition and transaction boundaries.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        users: UserRepository,
        sessions: SessionRepository,
        hasher: PasswordHasher,
        session_lifetime: timedelta = SESSION_LIFETIME,
    ):
        self._pool = pool
        self._users = users
        self._sessions = sessions
        self._hasher = hasher
        self._session_lifetime = session_lifetime

    @property
    def session_lifetime(self) -> timedelta:
        """How long an issued session lasts.

        Exposed for the cookie's Max-Age, which is a hint to the browser
        about when to stop sending the token. The server's answer is the
        `expires_at` column and nothing else; a client that ignores this and
        keeps presenting an expired token gets the same nothing as a client
        presenting a token that never existed.
        """
        return self._session_lifetime

    async def register(
        self,
        *,
        email: str,
        password: str,
        name: str | None,
    ) -> Authentication:
        """Create an account and sign it in.

        Signing in as part of registration, rather than making the client
        follow with a log-in, is a security choice as much as a convenience
        one: the alternative is the password crossing the network twice and
        living in the client for the round trip in between, to establish an
        identity the server has just finished establishing.

        Unlike log-in, this path does disclose that an address is taken. That
        is a real enumeration channel and it is accepted deliberately: a
        registration form that cannot say "you already have an account" sends
        people to reset a password they do not have, or to conclude the
        product is broken. The mitigations for it are rate limiting and
        eventually address verification, neither of which is this phase.
        """
        normalized = self._normalized_email(email)

        self._validate_registration(
            email=normalized,
            password=password,
            name=name,
        )

        # Hashed before a connection is acquired, never while holding one.
        # An argon2 hash takes on the order of 100ms, and the pool holds five
        # connections: hashing inside the `async with` below would let five
        # concurrent registrations hold the entire pool idle for a tenth of a
        # second each, starving every unrelated query in the process.
        password_hash = await self._hasher.hash(password)

        async with self._pool.acquire() as connection:
            # One transaction, because a user without a session is not a
            # halfway result -- it is an account whose owner was told they
            # were signed in and was not. Either both rows land or neither
            # does.
            async with connection.transaction():
                try:
                    user = await self._users.create(
                        connection,
                        email=normalized,
                        password_hash=password_hash,
                        name=self._normalized_name(name),
                    )
                except EmailAlreadyRegisteredError:
                    raise ValidationError(
                        [
                            ValidationIssue(
                                field="email",
                                code="EMAIL_TAKEN",
                                message="An account with this email already exists",
                            )
                        ]
                    ) from None

                issued = await self._issue_session(connection, user.id)

        return Authentication(user=user, issued=issued)

    async def log_in(self, *, email: str, password: str) -> Authentication:
        """Exchange a password for a session, or fail without saying why.

        Every failure below raises the same AuthenticationError, and the
        ordering exists so that they also take comparable time. An address
        with no account still costs a full argon2 verification, against a
        decoy hash, before failing -- otherwise the unknown-address case
        returns in microseconds and the wrong-password case in ~100ms, and
        the gap between them is a remote query for "does this person have an
        account here", answerable at whatever rate the network allows.

        The one early return that skips the hash is the length guard, and it
        leaks nothing: it depends only on the size of what the caller
        submitted, which the caller already knows. Without it a client can
        make the server carry an arbitrarily large string into a worker
        thread.
        """
        normalized = self._normalized_email(email)

        if len(normalized) > EMAIL_MAX_LENGTH or len(password) > PASSWORD_MAX_LENGTH:
            raise AuthenticationError()

        # Deliberately no format validation on this path. A malformed address
        # simply matches no row and fails like any other wrong answer;
        # reporting it as a validation error instead would give one class of
        # input a distinguishable, faster response for no benefit -- nobody
        # is helped by being told their log-in email is malformed when the
        # honest answer is that it does not work.
        async with self._pool.acquire() as connection:
            credentials = await self._users.find_credentials_by_email(
                connection,
                normalized,
            )

        # Outside the `async with`, for the reason given in `register`: a
        # pooled connection must not be held across a deliberately slow hash.
        if credentials is None:
            await self._hasher.verify_decoy(password)

            raise AuthenticationError()

        if not await self._hasher.verify(
            password_hash=credentials.password_hash,
            password=password,
        ):
            raise AuthenticationError()

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                user = await self._users.get_by_id(connection, credentials.user_id)

                if user is None:
                    # The account was deleted between reading its hash and
                    # here. A deleted account cannot be signed in to, so this
                    # is an authentication failure and not an internal one --
                    # nothing malfunctioned, the answer simply changed.
                    raise AuthenticationError()

                issued = await self._issue_session(connection, user.id)

        return Authentication(user=user, issued=issued)

    async def log_out(self, token: str | None) -> None:
        """End the session a token addresses, if it addresses one.

        Returns nothing, whatever happened. A caller presenting a token that
        matched no row learns only that they are now signed out, which was
        already true. Reporting the difference would confirm whether a token
        was ever real to whoever is holding it, and the one context where
        that question gets asked is not a legitimate one.

        A missing token is not an error either: logging out with no session
        is a request for a state the caller is already in.
        """
        if token is None:
            return

        async with self._pool.acquire() as connection:
            await self._sessions.delete_by_token_hash(
                connection,
                hash_session_token(token),
            )

    async def authenticate(self, token: str | None) -> UserEntity | None:
        """The user a session token identifies, or nothing.

        None covers every way a token can fail to identify somebody: absent,
        never issued, expired, or belonging to a deleted account. They are
        one answer on purpose -- the caller's next move is the same in all
        four cases, and a caller that could tell them apart would eventually
        report the difference to somebody.

        Two round trips, one to validate the session and one to read the
        user. They could be a single statement -- a CTE that updates the
        session and joins users on the way out -- and that is a real
        optimisation for a query on every authenticated request. It is not
        taken here because it puts a users query inside SessionRepository,
        and one saved round trip on the same already-open connection is not
        worth the layer it costs.
        """
        if token is None:
            return None

        token_hash = hash_session_token(token)

        async with self._pool.acquire() as connection:
            session = await self._sessions.touch_valid(connection, token_hash)

            if session is None:
                return None

            return await self._users.get_by_id(connection, session.user_id)

    async def _issue_session(
        self,
        connection: asyncpg.Connection,
        user_id: UUID,
    ) -> IssuedSession:
        """Mint a token, store its digest, and hand back the plaintext once.

        The only place a session token comes into existence. The plaintext
        goes into the returned value and nowhere else: it is not logged, not
        put in the payload, and not recoverable from the row, which holds
        only the digest.
        """
        token = generate_session_token()

        session = await self._sessions.create(
            connection,
            user_id=user_id,
            token_hash=hash_session_token(token),
            lifetime=self._session_lifetime,
        )

        return IssuedSession(token=token, session=session)

    @staticmethod
    def _normalized_email(email: str) -> str:
        """Case-folded, and nothing else.

        Folding is not tidying: two spellings differing only in case are one
        account, so this is what makes users_email_key mean what the product
        needs it to mean.

        Surrounding whitespace is deliberately NOT stripped. Trimming would
        mean the server storing something other than what was submitted and
        reporting success, and " a@b.com" is a client-side input problem with
        a client-side fix. The shape check rejects it and says so.
        """
        return email.lower()

    @staticmethod
    def _normalized_name(name: str | None) -> str | None:
        """An empty name and no name are the same fact, stored one way.

        Left as "" the column would hold two representations of "this person
        gave no name", and every reader would have to handle both or handle
        one and be wrong occasionally.
        """
        if name is None or not name:
            return None

        return name

    @staticmethod
    def _validate_registration(
        *,
        email: str,
        password: str,
        name: str | None,
    ) -> None:
        """Collect every violation, then raise once.

        Field order is deterministic (email, password, name) so that clients
        can rely on it. The codes and messages are a public contract.

        The password's own text never appears in an issue -- only facts about
        its length. A message that quoted it would put a password into a
        GraphQL response, an error log and a browser's network tab at once.
        """
        issues: list[ValidationIssue] = []

        # REQUIRED and INVALID are kept apart deliberately. "You left this
        # blank" and "this is not an address" are different corrections, and
        # a form that renders the second for an empty field reads as broken.
        if not email:
            issues.append(
                ValidationIssue(
                    field="email",
                    code="REQUIRED",
                    message="Email is required",
                )
            )
        elif len(email) > EMAIL_MAX_LENGTH:
            issues.append(
                ValidationIssue(
                    field="email",
                    code="TOO_LONG",
                    message=f"Email must be at most {EMAIL_MAX_LENGTH} characters",
                )
            )
        elif EMAIL_PATTERN.match(email) is None:
            issues.append(
                ValidationIssue(
                    field="email",
                    code="INVALID",
                    message="Email is not a valid address",
                )
            )

        if len(password) < PASSWORD_MIN_LENGTH:
            issues.append(
                ValidationIssue(
                    field="password",
                    code="TOO_SHORT",
                    message=(
                        f"Password must be at least {PASSWORD_MIN_LENGTH} characters"
                    ),
                )
            )
        elif len(password) > PASSWORD_MAX_LENGTH:
            issues.append(
                ValidationIssue(
                    field="password",
                    code="TOO_LONG",
                    message=(
                        f"Password must be at most {PASSWORD_MAX_LENGTH} characters"
                    ),
                )
            )

        if name is not None and len(name) > NAME_MAX_LENGTH:
            issues.append(
                ValidationIssue(
                    field="name",
                    code="TOO_LONG",
                    message=f"Name must be at most {NAME_MAX_LENGTH} characters",
                )
            )

        if issues:
            raise ValidationError(issues)
