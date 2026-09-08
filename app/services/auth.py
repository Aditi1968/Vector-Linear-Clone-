import asyncio
import hashlib
import re
from datetime import timedelta
from uuid import UUID

import asyncpg
import structlog

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
from app.repositories.rate_limits import RateLimitRepository
from app.repositories.sessions import SessionRepository
from app.repositories.users import UserRepository
from app.services.passwords import PasswordHasher
from app.services.tokens import generate_session_token, hash_session_token


logger = structlog.get_logger(__name__)


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


# --- Rate limiting ---------------------------------------------------------
#
# migrations/032_auth_hardening.sql argues for the table and for the fixed
# window. This block is the policy: which key, how many, and what a refusal
# says. See app/repositories/rate_limits.py, which counts and decides nothing.
#
#
# TWO KEYS, AND WHICH ONE IS THE REAL LIMIT
#
# The address bucket is the limit. It is the only key that bounds guessing
# against a particular account, because that is the thing an attacker holds
# constant while everything else about their traffic varies.
#
# The IP bucket is a backstop with a deliberately loose budget, and it is loose
# because `app/http_client_ip.py` cannot promise the value is the caller's. The
# shapes this repository deploys -- a NodePort Service that SNATs, or the
# Ingress its own manifest recommends -- can collapse every external client
# onto one address. A tight budget on a key that might be one value for the
# whole internet is not a rate limit, it is an outage with a schedule. So the
# IP budget is sized to stop a flood rather than to pace a user, and a request
# with no usable address simply has no IP bucket rather than being refused.
#
# It still earns its place: an address bucket cannot see credential stuffing,
# where one password is tried against thousands of DIFFERENT accounts and no
# single address bucket reaches two.
#
#
# THE LOCKOUT THIS INTRODUCES, WHICH IS REAL AND IS ACCEPTED
#
# Keying on the submitted address means anyone who knows an address can spend
# its budget, and the owner is then refused for the rest of the window with a
# message that says their password is wrong. That is a denial of service
# against one account, by design, and there is no key that both bounds guessing
# against an account and cannot be spent by a stranger -- keying on (address,
# IP) would fix it and would also hand a botnet a fresh budget per host, which
# is the attack rather than the fumble.
#
# Three things bound the damage, and they are the reason this is the trade
# taken rather than a hole left open:
#
#   * it expires. RATE_LIMIT_WINDOW, not an administrative unlock;
#   * a correct password clears it. `RateLimitRepository.clear` runs in the
#     transaction that issues the session, so a user who fumbles four times and
#     then succeeds is back at zero rather than four from the edge;
#   * the budget is far above human error. Ten wrong passwords in a quarter of
#     an hour is not somebody mistyping.
#
#   ponytail: no unlock path and no notification to the account owner. Both are
#   the right upgrade -- a "we blocked some attempts" email is the standard
#   answer -- and both need an outbound mail path this application does not
#   have yet. The upgrade path is `domain_events` plus a mail adapter, at which
#   point this comment shrinks to a pointer at it.
RATE_LIMIT_WINDOW = timedelta(minutes=15)

# The three budgets, as scope -> attempts inside one window. The scope strings
# are the `scope` column; migration 032 explains why the operation and the key
# kind are one value.
#
# 10 for an address: above any plausible fumble, and slow enough that a
# dictionary is useless -- 40 guesses an hour against one account.
#
# 100 for an address that may be a whole office behind one NAT, or every
# external client behind one SNAT. It is a flood ceiling: 400 log-in attempts
# an hour from one address is not a workday, and this deployment -- two
# replicas, a 512Mi limit -- does not serve that many real sign-ins.
#
# 20 for registration, which is the budget that answers `register`'s
# documented enumeration channel. Registering discloses EMAIL_TAKEN on purpose;
# 20 an hour makes reading the user list out of that disclosure take longer than
# it is worth, without inconveniencing anybody who is signing up once.
#
# All three are doubled at a window boundary -- the known fixed-window flaw,
# priced in by migration 032 and by these numbers being ceilings rather than
# thresholds.
LOGIN_IP_SCOPE = "login:ip"
LOGIN_EMAIL_SCOPE = "login:email"
REGISTER_IP_SCOPE = "register:ip"

LOGIN_BUDGETS = {
    LOGIN_EMAIL_SCOPE: 10,
    LOGIN_IP_SCOPE: 100,
}

REGISTER_BUDGETS = {
    REGISTER_IP_SCOPE: 20,
}

# What a rate-limited REGISTRATION reports, and the one refusal in this file
# that is allowed to say what it is.
#
# Registration already discloses that an address is taken -- that is the
# channel this budget exists to slow, not a secret this message could spoil --
# and the register bucket is keyed on the caller's own address and nothing
# else, so the answer depends only on what the caller themselves has recently
# done. There is nothing here for anyone to learn about anybody else, and a
# person who is genuinely signing up deserves to be told to wait rather than
# left to conclude the product is broken.
#
# Contrast `log_in`, which must not say this. See its docstring.
TOO_MANY_ATTEMPTS = ValidationIssue(
    field="email",
    code="TOO_MANY_ATTEMPTS",
    message="Too many attempts. Try again later.",
)


# --- The sweep -------------------------------------------------------------

# How often a process runs `AuthService.sweep_once`.
#
# Fifteen minutes, which is RATE_LIMIT_WINDOW, and matching them is the whole
# argument: a bucket becomes collectable exactly one window after its last
# attempt, so collecting on that cadence keeps the table's steady-state size at
# roughly the subjects active in the last two windows rather than every subject
# that ever attempted anything. `sessions` needs nothing like this pace -- a
# session lives fourteen days -- but it is the same pass, and an indexed DELETE
# that matches nothing costs a plan and a round trip.
#
# Neither table is load-bearing on this timer. A stale bucket is already
# harmless: `RateLimitRepository.consume` rolls it over without consulting the
# sweep, and `SessionRepository.touch_valid` has always refused an expired row.
# The sweep can fall arbitrarily far behind, or not run at all, without
# changing a single authentication decision. It reclaims space.
SWEEP_INTERVAL_SECONDS = 900.0

# Rows per statement. The pass loops until a batch comes back short, so this is
# not a cap on what a pass collects -- it is the size of each transaction, and
# it is bounded for the reason `SessionRepository.delete_expired` gives: the
# first run against a table nobody has ever swept could otherwise be one
# transaction holding locks over every dead row in it.
SWEEP_BATCH = 1000


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
        rate_limits: RateLimitRepository,
        session_lifetime: timedelta = SESSION_LIFETIME,
    ):
        self._pool = pool
        self._users = users
        self._sessions = sessions
        self._hasher = hasher
        # Required rather than defaulted to `RateLimitRepository()`, and that
        # is not consistency for its own sake. A default would mean a caller
        # who assembled this service by hand got a limiter silently, or -- once
        # somebody made the default None to keep an old call site compiling --
        # got an unlimited auth surface silently. The budgets are the defence;
        # whether they are wired has to be visible at every composition root.
        self._rate_limits = rate_limits
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
        client_ip: str | None,
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
        product is broken. The mitigation this docstring used to promise -- "a
        later phase" -- is the budget below; address verification is still
        owed.

        `client_ip` is the caller's address or None, from
        `app.http_client_ip.read_client_ip`, and it is a required argument so
        that no call site can acquire an unlimited registration path by
        forgetting one. None means this deployment could not establish an
        address, and it means this path has NO budget: enumeration walks a
        different address every time, so the submitted email is a key that
        never repeats and would never trip. IP is the only key that sees the
        sweep, so no IP is no limit -- which is why `read_client_ip` falls back
        to the TCP peer rather than giving up.
        """
        normalized = self._normalized_email(email)

        # Validated before anything is counted, so that a typo does not spend
        # budget. Nothing before this point touches the database or the hasher,
        # so an unbounded stream of malformed input is refused for free -- it
        # is bounded by the request body limit, not by this table.
        self._validate_registration(
            email=normalized,
            password=password,
            name=name,
        )

        # Counted before the hash, in an acquisition of its own that is
        # released before it. That ordering is the point of the whole
        # exercise: a budget consumed after a ~100ms argon2 operation is a
        # budget on the reply rather than on the work, and the work is what an
        # attacker is trying to buy.
        #
        # Deliberately NOT inside a transaction. An attempt has to stay counted
        # whether or not the registration it belongs to goes on to succeed, and
        # a rollback that gave the budget back would give it back on exactly
        # the failures worth counting.
        async with self._pool.acquire() as connection:
            attempts = await self._rate_limits.consume(
                connection,
                buckets=self._register_buckets(client_ip),
                window=RATE_LIMIT_WINDOW,
            )

        if _over_budget(attempts, REGISTER_BUDGETS):
            raise ValidationError([TOO_MANY_ATTEMPTS])

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

    async def log_in(
        self,
        *,
        email: str,
        password: str,
        client_ip: str | None,
    ) -> Authentication:
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

        THE RATE-LIMIT REFUSAL IS THE SAME ERROR, and that is the sentence
        this whole method is arranged around. A distinguishable "you are rate
        limited" would be a new answer that varies with something other than
        the caller's own submission: an attacker who spends one attempt on an
        address and is told it is throttled has learned that somebody has been
        failing log-ins against it recently, which is a signal about a real
        account -- and it is precisely the class of signal the decoy hash pays
        100ms per request to erase. So a refused attempt returns exactly what a
        wrong password returns, and the only person inconvenienced is the one
        who cannot be told why. That cost is accepted; see RATE_LIMIT_WINDOW
        for the ponytail note on the notification that should eventually
        replace it.

        The refusal IS faster, because it skips the hash, and that asymmetry is
        deliberate and harmless. Spending an argon2 operation to disguise the
        refusal would reinstate the amplifier the budget exists to remove --
        and what the timing reveals is only "you are being throttled", which
        the caller can already work out by counting their own requests. It
        reveals nothing about whether the address has an account, because the
        bucket is keyed on the SUBMITTED address and is created for addresses
        that match nothing exactly as readily as for ones that do.
        """
        normalized = self._normalized_email(email)

        if len(normalized) > EMAIL_MAX_LENGTH or len(password) > PASSWORD_MAX_LENGTH:
            raise AuthenticationError()

        # Held for the success path too, so that a log-in that works clears
        # the same buckets it consumed. Built once because
        # `_subject_for_email` is a digest and computing it twice would be two
        # chances to disagree about what was counted.
        buckets = self._login_buckets(normalized, client_ip)

        # Deliberately no format validation on this path. A malformed address
        # simply matches no row and fails like any other wrong answer;
        # reporting it as a validation error instead would give one class of
        # input a distinguishable, faster response for no benefit -- nobody
        # is helped by being told their log-in email is malformed when the
        # honest answer is that it does not work.
        async with self._pool.acquire() as connection:
            # First, and outside any transaction: an attempt counts whether or
            # not it succeeds, and a rollback would refund the budget on
            # exactly the failures worth counting. One extra statement on the
            # connection this method was already going to take.
            attempts = await self._rate_limits.consume(
                connection,
                buckets=buckets,
                window=RATE_LIMIT_WINDOW,
            )

            if _over_budget(attempts, LOGIN_BUDGETS):
                # Before the credential lookup, not merely before the hash.
                # A refused attempt must not read `users` at all: that is the
                # one statement here whose cost varies with whether the
                # address exists, and leaving it in would keep a measurable
                # difference on the path that skips everything else.
                raise AuthenticationError()

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

                # In the same transaction as the session, so the budget is
                # forgiven only if the sign-in it belongs to actually
                # happened. This is what keeps the address bucket from being a
                # slow lockout of everybody who types badly: getting it right
                # puts them back at zero rather than one fumble from the edge.
                await self._rate_limits.clear(connection, buckets=buckets)

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

    async def sweep_once(self, *, batch: int = SWEEP_BATCH) -> tuple[int, int]:
        """Collect lapsed sessions and aged-out buckets. Returns both counts.

        Space, and only space. Nothing this deletes is consulted by any
        decision: `touch_valid` has always refused an expired session in its
        WHERE clause, and `RateLimitRepository.consume` rolls a stale bucket
        over to 1 inside its own upsert. A deployment where this never ran
        would authenticate identically and grow forever, which is the defect
        rather than the risk.

        SAFE ON BOTH REPLICAS, with no lease, no advisory lock and no `FOR
        UPDATE SKIP LOCKED` -- and the argument is `SessionRepository.
        delete_expired`'s, which is worth repeating because migrations 027 and
        028 reached the opposite conclusion for their own queues. Those two
        lease a row so that exactly one worker performs an expensive, external,
        NON-IDEMPOTENT action with it: a model run, a Slack post. Here the
        statement IS the work and it is idempotent. Two replicas sweeping at
        once take row locks in physical order; the loser waits, finds the rows
        gone under READ COMMITTED, and returns having left the table in the
        one state either of them intended. The only thing a lock would buy is
        a tidier number in the log line.

        So this is started unconditionally rather than behind a flag like the
        embedding worker. That flag exists because model work is expensive and
        somebody has to choose where it runs; two indexed DELETEs that usually
        match nothing need no such decision, and a flag would mean a
        deployment that forgot it grows both tables forever with nothing
        saying so.

        LOOPS UNTIL A PASS COMES BACK SHORT, acquiring per batch rather than
        holding one connection across all of them. A first run against a table
        nobody has ever swept can be many batches, and the pool has five
        connections: keeping one for the duration would take a fifth of it out
        of service for as long as the backlog lasts. Releasing between batches
        also gives every concurrent request a turn, which is what makes a
        catch-up pass invisible rather than merely bounded.

        It terminates. Both predicates match a set that is closed at the
        instant the pass begins -- a session expires on a fourteen-day clock, a
        bucket ages out on a fifteen-minute one -- so rows written during the
        sweep do not join the set the sweep is draining.
        """
        sessions = 0

        while True:
            async with self._pool.acquire() as connection:
                deleted = await self._sessions.delete_expired(connection, batch=batch)

            sessions += deleted

            if deleted < batch:
                break

        limits = 0

        while True:
            async with self._pool.acquire() as connection:
                deleted = await self._rate_limits.delete_stale(
                    connection,
                    window=RATE_LIMIT_WINDOW,
                    batch=batch,
                )

            limits += deleted

            if deleted < batch:
                break

        return sessions, limits

    @staticmethod
    def _login_buckets(
        normalized_email: str,
        client_ip: str | None,
    ) -> list[tuple[str, str]]:
        """The buckets one log-in attempt spends, in the order they are read.

        The address bucket is always present; the IP bucket only when there is
        an address to key it on. A missing IP degrades the limit rather than
        disabling it, which is the correct failure for a value
        `app.http_client_ip` is explicit about not being able to guarantee.
        """
        buckets = [(LOGIN_EMAIL_SCOPE, _subject_for_email(normalized_email))]

        if client_ip is not None:
            buckets.append((LOGIN_IP_SCOPE, client_ip))

        return buckets

    @staticmethod
    def _register_buckets(client_ip: str | None) -> list[tuple[str, str]]:
        """The buckets one registration spends. IP only; see `register`."""
        if client_ip is None:
            return []

        return [(REGISTER_IP_SCOPE, client_ip)]

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


def _subject_for_email(normalized_email: str) -> str:
    """The bucket key for an address: its SHA-256, hex.

    THE ONE PLACE AN ADDRESS BECOMES A SUBJECT, which is what lets
    `app.repositories.rate_limits` say that no statement it holds can be
    handed a raw address by accident -- none of them hashes anything, so the
    only way in is through here.

    A digest because the alternative is a second copy of the user list, plus
    every address anyone ever guessed at, in a table with no tenant and no
    foreign key. The limiter only ever needs equality. Not salted and not
    peppered: a keyed digest would protect against someone who can already read
    this table testing a guessed address against it, which is a real but much
    smaller concern than the table existing in plaintext, and it would need a
    key with a rotation story attached to a value that is meaningless fifteen
    minutes after it is written.

    Takes an ALREADY-NORMALISED address. Digesting is case-sensitive where
    `users_email_key` is not, so hashing the raw submission would give
    "Ada@example.com" and "ada@example.com" separate budgets -- two spellings
    of one account, and a free doubling for anyone who noticed.
    """
    return hashlib.sha256(normalized_email.encode("utf-8")).hexdigest()


def _over_budget(attempts: dict[str, int], budgets: dict[str, int]) -> bool:
    """Whether any bucket this attempt touched is now past its budget.

    Reads only the scopes that were actually consumed, so a caller that had no
    IP to key on is judged on the buckets it has rather than failing a lookup
    for one it never spent. `RateLimitRepository.consume` returns exactly the
    scopes it was given.

    Strictly greater than, so a budget of 10 permits the tenth attempt and
    refuses the eleventh -- the count is post-increment, and off by one here
    would be a limit that is quietly one tighter than the number written above
    it.
    """
    return any(count > budgets[scope] for scope, count in attempts.items())


async def run_sweep_loop(
    auth: AuthService,
    *,
    interval: float = SWEEP_INTERVAL_SECONDS,
) -> None:
    """Run `sweep_once` forever, at a pace nothing can make faster.

    The same shape as `app.services.notifications.run_delivery_loop`, and for
    the same reasons, so the two are worth keeping identical: the sleep is
    AFTER the pass and OUTSIDE the try, so it happens on every path including
    the one where the first statement raises -- a database refusing every
    connection is otherwise a tight loop of failed connects with a log line
    each.

    Every exception except cancellation is caught and logged. A supervisor loop
    has no supervisor above it: propagating ends the task, nothing restarts it,
    and both tables silently resume growing forever for the life of the
    process. `CancelledError` derives from BaseException and so is not caught
    by that clause, which is what lets `app.main.lifespan` actually shut down
    rather than hang on a task that logs its own cancellation and goes round
    again.

    Logged only when it collected something, because the honest steady state is
    a pass that deletes nothing every fifteen minutes, and 96 lines a day
    saying "0, 0" is how a log stops being read.
    """
    while True:
        try:
            sessions, rate_limits = await auth.sweep_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("auth.sweep.pass_failed")
        else:
            if sessions or rate_limits:
                logger.info(
                    "auth.sweep.collected",
                    sessions=sessions,
                    rate_limits=rate_limits,
                )

        await asyncio.sleep(interval)
