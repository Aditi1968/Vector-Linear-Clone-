from datetime import timedelta
from uuid import UUID

import asyncpg

from app.domain.auth import SessionEntity


class SessionRepository:
    """SQL access for `sessions`.

    The repository receives a connection from the service layer. It never
    acquires connections, never touches the pool, and never owns a
    transaction. `asyncpg.Record` never escapes this class.

    Every method here takes a `token_hash`, never a token. The raw token is
    hashed by the service before it gets this far, so no query in this file
    can bind one and no session token can reach the wire protocol, the query
    log, or a `pg_stat_activity` row.
    """

    async def create(
        self,
        connection: asyncpg.Connection,
        *,
        user_id: UUID,
        token_hash: bytes,
        lifetime: timedelta,
    ) -> SessionEntity:
        """Record a session against an already-computed digest.

        A lifetime, not an expiry instant, so that `expires_at` is computed
        from the same clock `touch_valid` compares it against. Handing in a
        Python-computed timestamp would make a session's real duration depend
        on the drift between the application host and the database: an
        application clock a minute fast silently issues sessions a minute
        short, and one a minute slow issues them long, with nothing in either
        case to reveal it. `now()` on both sides removes the question.

        How long a session lasts is still a policy decision and still the
        service's: it chooses the timedelta, this only arithmetics it.
        """
        row = await connection.fetchrow(
            """
            INSERT INTO sessions (
                user_id,
                token_hash,
                expires_at
            )
            VALUES ($1, $2, now() + $3::interval)
            RETURNING
                id,
                user_id,
                created_at,
                last_used_at,
                expires_at
            """,
            user_id,
            token_hash,
            lifetime,
        )

        return self._to_entity(row)

    async def touch_valid(
        self,
        connection: asyncpg.Connection,
        token_hash: bytes,
    ) -> SessionEntity | None:
        """The live session for a digest, stamped as used; or nothing.

        Expiry is decided here, in the WHERE clause, and not by the caller
        comparing timestamps afterwards. Two reasons, and the second is the
        one that matters. `now()` is the database's clock, so every process
        in a deployment agrees about when a session ended regardless of how
        far their own clocks have drifted. And a predicate cannot be
        forgotten: a caller-side check is one early `return` away from
        handing out an expired session, whereas a query that does not match
        cannot return a row to be mishandled.

        Validation is a write, which is the cost of `last_used_at` meaning
        anything. It is one HOT update on a narrow row found through a unique
        index, in the same round trip as the lookup, so it adds no latency --
        but it does dirty a row per authenticated request, which is
        autovacuum's problem at volume. The fix, if it becomes one, is a
        staleness predicate plus a SELECT fallback for the sessions already
        touched recently; that is two statements and belongs behind a
        measurement rather than ahead of one.

        A digest matching an expired row and one matching nothing at all both
        return None, and the caller must not be able to tell them apart: the
        difference is exactly "this token was real once", which is not
        something to confirm to whoever presented it.
        """
        row = await connection.fetchrow(
            """
            UPDATE sessions
            SET last_used_at = now()
            WHERE token_hash = $1
              AND expires_at > now()
            RETURNING
                id,
                user_id,
                created_at,
                last_used_at,
                expires_at
            """,
            token_hash,
        )

        if row is None:
            return None

        return self._to_entity(row)

    async def delete_by_token_hash(
        self,
        connection: asyncpg.Connection,
        token_hash: bytes,
    ) -> bool:
        """Delete a session, reporting whether there was one to delete.

        No expiry predicate. Logout means the row must be gone, and an
        expired row is still a row: leaving it because it had already lapsed
        would keep a digest in the table that nothing will ever clean up
        except a sweep that does not exist yet.

        The boolean is for the caller's own logic, not for the client. Whether
        a presented token matched anything is the same fact `touch_valid`
        refuses to disclose, and logout answers the same way whatever this
        returns.
        """
        # `execute` returns the command tag -- "DELETE 1" or "DELETE 0" --
        # which is how a plain DELETE reports its row count. RETURNING would
        # work too and would pull back an id nothing needs.
        #
        # Annotated because asyncpg ships no types: the tag is Any, and an
        # unannotated comparison would satisfy any return type at all.
        status: str = await connection.execute(
            """
            DELETE FROM sessions
            WHERE token_hash = $1
            """,
            token_hash,
        )

        return status != "DELETE 0"

    @staticmethod
    def _to_entity(row: asyncpg.Record) -> SessionEntity:
        return SessionEntity(
            id=row["id"],
            user_id=row["user_id"],
            created_at=row["created_at"],
            last_used_at=row["last_used_at"],
            expires_at=row["expires_at"],
        )
