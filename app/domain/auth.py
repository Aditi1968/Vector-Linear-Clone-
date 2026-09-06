from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class UserEntity:
    """A user account, as everything above the repository sees one.

    There is no `password_hash` field, and that omission is the point. This
    is the type that travels out through services into resolvers and gets
    turned into a GraphQL object, so any secret it carried would be one
    careless field away from a response body. The hash exists only inside
    `UserCredentials`, which the login path reads and nothing else does.

    Pure application code -- no Strawberry, FastAPI, asyncpg or PostgreSQL.
    """

    id: UUID
    email: str
    name: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class UserCredentials:
    """What is needed to check a password, and nothing more.

    Deliberately not a UserEntity with an extra field. A login attempt needs
    the stored hash and the id to issue a session against; it has no use for
    the name or the timestamps, and a type that carried them would be
    tempting to return from somewhere other than the one method that must.

    Read by AuthService.log_in and by nothing else. It never reaches a
    resolver, a payload or a log line.
    """

    user_id: UUID
    password_hash: str


@dataclass(frozen=True, slots=True)
class SessionEntity:
    """A server-side session, without the token that addresses it.

    Same reasoning as UserEntity: the stored digest is not here, and neither
    is the raw token. Holding a SessionEntity lets code say which user is
    authenticated and until when; it does not let that code re-issue,
    replay, or leak the credential the session was found by.
    """

    id: UUID
    user_id: UUID
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class IssuedSession:
    """A session and the one copy of its raw token that will ever exist.

    The token is generated, hashed, and the hash is what the database gets;
    the plaintext is returned here and travels exactly one hop, from
    AuthService to the code that writes the Set-Cookie header. Nothing
    persists it, nothing logs it, and no query can produce it again -- a
    lost token is a session that can only be waited out or deleted.

    Kept as a distinct type rather than a (str, SessionEntity) tuple so that
    the secret is named wherever it is passed, and so that returning a bare
    SessionEntity from a function declared to return this one does not
    type-check.
    """

    token: str
    session: SessionEntity


@dataclass(frozen=True, slots=True)
class Authentication:
    """The outcome of a successful register or log-in.

    Both operations end the same way -- an identity, and a fresh session
    standing in for the password from here on -- so both return this, and
    the resolver that writes the Set-Cookie header does not need to know
    which one it is handling.
    """

    user: UserEntity
    issued: IssuedSession
