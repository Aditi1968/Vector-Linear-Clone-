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
        except `delete_expired` below, which runs on a timer and not on the
        request the user is making right now.

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

    async def delete_expired(
        self,
        connection: asyncpg.Connection,
        *,
        batch: int,
    ) -> int:
        """Remove lapsed sessions, up to `batch` of them, and say how many.

        Not a security fix. `touch_valid` has always refused an expired row, so
        nothing here changes who can authenticate; migrations/003_auth.sql even
        created `sessions_expires_at_idx` for a sweep it correctly predicted
        would be needed and that nobody had written. What was broken is that
        the table only ever grew -- one row per log-in, forever, each holding a
        digest of a credential that stopped meaning anything a fortnight ago.

        BOUNDED, and that is the whole reason this takes an argument. The first
        run against a table that has never been swept could match every row in
        it, and an unbounded DELETE is then one transaction holding row locks
        over all of them, bloating WAL and blocking the `touch_valid` UPDATE
        that every authenticated request makes. A batch turns that into a
        series of short transactions the caller can pace; see
        `AuthService.sweep_once`, which loops until a pass comes back short.

        `now()` and not a caller's timestamp, so the sweep and the predicate in
        `touch_valid` are deciding expiry off one clock. A Python-computed
        cutoff on a host running fast would delete sessions that this database
        still considers live, which is signing people out at random.

        SAFE ON TWO REPLICAS WITH NO `FOR UPDATE SKIP LOCKED`, unlike the
        claims in migrations/027 and 028, and the difference is what the
        statement leads to. Those two lease a row so that exactly one worker
        performs an expensive, external, non-idempotent action with it -- a
        model run, a Slack post. Here the statement IS the work, and it is
        idempotent: a row deleted twice is a row deleted. Two sweeps racing
        take row locks in physical order, the loser waits and then finds the
        rows already gone under READ COMMITTED, and both return having left the
        table in the one state either intended. SKIP LOCKED would only mean the
        loser reports a smaller number for the same outcome.
        """
        status: str = await connection.execute(
            """
            DELETE FROM sessions
            WHERE id IN (
                SELECT id
                FROM sessions
                WHERE expires_at <= now()
                LIMIT $1
            )
            """,
            batch,
        )

        # asyncpg ships no types for `execute`, so the tag is Any; annotating
        # above and parsing here is what stops an unannotated read satisfying
        # any return type at all.
        return int(status.split()[-1])

    @staticmethod
    def _to_entity(row: asyncpg.Record) -> SessionEntity:
        return SessionEntity(
            id=row["id"],
            user_id=row["user_id"],
            created_at=row["created_at"],
            last_used_at=row["last_used_at"],
            expires_at=row["expires_at"],
        )
