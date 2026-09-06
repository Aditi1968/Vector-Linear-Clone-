from uuid import UUID

import asyncpg

from app.domain.auth import UserCredentials, UserEntity
from app.domain.errors import EmailAlreadyRegisteredError


# The unique constraint migration 003 declares on users.email. Named here
# because this is the layer that knows what the schema calls things, and
# compared by name because a UniqueViolationError alone does not say which
# constraint fired -- a future unique index on this table would raise the
# same class, and reporting it as "email already registered" would be a lie
# the caller has no way to detect.
EMAIL_UNIQUE_CONSTRAINT = "users_email_key"


class UserRepository:
    """SQL access for `users`.

    The repository receives a connection from the service layer. It never
    acquires connections, never touches the pool, and never owns a
    transaction. `asyncpg.Record` never escapes this class.

    It also never receives a password. The service hashes before it calls
    here, so a plaintext password is not a value any query in this file
    could bind even by mistake. `password_hash` is read by exactly one
    method below, which returns a type that carries nothing else.
    """

    async def create(
        self,
        connection: asyncpg.Connection,
        *,
        email: str,
        password_hash: str,
        name: str | None,
    ) -> UserEntity:
        """Insert a user, or report that the address is taken.

        There is no SELECT-then-INSERT here, and that is the point. Checking
        for an existing address first leaves a window between the check and
        the insert in which another registration can commit, so under
        concurrency it either produces the unique violation anyway or -- if
        the code trusts its own check -- reports success for a row that was
        never written. The constraint is the only thing that can decide this
        atomically, so the insert is attempted and its refusal is the answer.

        `email` is expected already folded to lowercase; users_email_lowercase
        rejects anything else rather than storing a second spelling of an
        address that already has an account.
        """
        try:
            row = await connection.fetchrow(
                """
                INSERT INTO users (
                    email,
                    password_hash,
                    name
                )
                VALUES ($1, $2, $3)
                RETURNING
                    id,
                    email,
                    name,
                    created_at,
                    updated_at
                """,
                email,
                password_hash,
                name,
            )
        except asyncpg.UniqueViolationError as exc:
            if exc.constraint_name != EMAIL_UNIQUE_CONSTRAINT:
                raise

            # `from None`: asyncpg's error carries the offending row's values
            # in its `detail`, which includes the address. Chaining it would
            # carry that into every traceback and log line above here.
            raise EmailAlreadyRegisteredError() from None

        return self._to_entity(row)

    async def find_credentials_by_email(
        self,
        connection: asyncpg.Connection,
        email: str,
    ) -> UserCredentials | None:
        """The stored hash for an address, or nothing.

        The only query in the codebase that reads password_hash, and it
        returns a type that holds nothing but the hash and the id it belongs
        to -- so a hash cannot reach a payload by being a field on an object
        somebody serialised.

        The comparison is exact, with no LOWER() around the column: every
        stored address is already lowercase by constraint, so folding here
        would only defeat users_email_key's index without matching anything
        extra. The caller folds the input instead.

        No LIMIT 1: users_email_key makes a second matching row impossible,
        and a limit would claim doubt about a guarantee the schema gives.
        """
        row = await connection.fetchrow(
            """
            SELECT
                id,
                password_hash
            FROM users
            WHERE email = $1
            """,
            email,
        )

        if row is None:
            return None

        # Annotated rather than returned inline: asyncpg ships no types, so
        # `row["id"]` is Any and would silently satisfy any field type.
        user_id: UUID = row["id"]
        password_hash: str = row["password_hash"]

        return UserCredentials(user_id=user_id, password_hash=password_hash)

    async def get_by_id(
        self,
        connection: asyncpg.Connection,
        user_id: UUID,
    ) -> UserEntity | None:
        row = await connection.fetchrow(
            """
            SELECT
                id,
                email,
                name,
                created_at,
                updated_at
            FROM users
            WHERE id = $1
            """,
            user_id,
        )

        if row is None:
            return None

        return self._to_entity(row)

    @staticmethod
    def _to_entity(row: asyncpg.Record) -> UserEntity:
        return UserEntity(
            id=row["id"],
            email=row["email"],
            name=row["name"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
