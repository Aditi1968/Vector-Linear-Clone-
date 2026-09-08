from collections.abc import Sequence
from datetime import timedelta

import asyncpg


class RateLimitRepository:
    """SQL over `auth_rate_limits`, the shared counter in front of the auth
    surface.

    The repository receives a connection from the service layer. It never
    acquires connections, never touches the pool, and never owns a
    transaction. `asyncpg.Record` never escapes this class.

    Nothing here decides anything. It counts, it reports the count, and the
    budgets live in `app.services.auth` -- which is where "twenty log-ins per
    quarter hour" is a policy somebody can argue with rather than a literal
    buried in a query. This class does not know what a limit is.

    It also does not know what a subject means. `subject` arrives already
    normalised and, for an email, already digested; see
    `app.services.auth._subject_for_email`. No statement here can be passed a
    raw address by accident, because none of them hashes anything.

    NO TENANCY, and there is nothing to scope. Every row is written for an
    unauthenticated caller who by definition has no workspace, and nothing
    read back reaches a client -- the service turns a count into a refusal and
    the refusal says nothing about the count. See
    migrations/032_auth_hardening.sql.
    """

    async def consume(
        self,
        connection: asyncpg.Connection,
        *,
        buckets: Sequence[tuple[str, str]],
        window: timedelta,
    ) -> dict[str, int]:
        """Count one attempt against each bucket, and report the new counts.

        Keyed by scope in the result, which is unambiguous because a caller
        passes each scope at most once per attempt: an attempt has one IP and
        one address, so 'login:ip' and 'login:email' are two buckets and never
        two of either. A caller that passed a scope twice would get one of its
        counts, which is a bug in the caller and not a state this table can be
        left in.

        One statement for all of them rather than one round trip each. The two
        buckets a log-in consumes are one fact about one attempt, and splitting
        them would mean an attempt that incremented the IP counter and then
        failed to increment the address counter -- half a count, on the path
        whose entire job is to count.

        THE UPSERT IS THE ATOMICITY. `ON CONFLICT DO UPDATE` takes the row lock
        before it reads `attempts`, so two replicas incrementing the same
        subject in the same instant serialise into 1 then 2, never 1 and 1. A
        SELECT followed by an UPDATE would be the lost update this table exists
        to prevent, and it would lose it precisely under the concurrency an
        attacker supplies for free.

        THE WINDOW ROLLS INSIDE THE SAME STATEMENT. A bucket whose window
        opened longer ago than `window` restarts at 1 with a fresh
        `window_started_at`; anything newer increments in place. Doing it here
        rather than in a preceding DELETE is what keeps the whole thing one
        round trip and one lock -- and it means an expired bucket needs no
        sweep to become usable again. The sweep only reclaims space.

        `now()` on both sides, never a Python timestamp, for the reason
        `SessionRepository.create` gives: two replicas with drifting clocks
        must not disagree about when a window rolls.
        """
        if not buckets:
            return {}

        scopes = [scope for scope, _ in buckets]
        subjects = [subject for _, subject in buckets]

        rows = await connection.fetch(
            """
            INSERT INTO auth_rate_limits AS l (
                scope,
                subject,
                window_started_at,
                attempts
            )
            SELECT
                bucket.scope,
                bucket.subject,
                now(),
                1
            FROM unnest($1::text[], $2::text[]) AS bucket (scope, subject)
            ON CONFLICT (scope, subject) DO UPDATE
            SET
                attempts = CASE
                    WHEN l.window_started_at <= now() - $3::interval THEN 1
                    ELSE l.attempts + 1
                END,
                window_started_at = CASE
                    WHEN l.window_started_at <= now() - $3::interval THEN now()
                    ELSE l.window_started_at
                END
            RETURNING l.scope, l.attempts
            """,
            scopes,
            subjects,
            window,
        )

        return {row["scope"]: row["attempts"] for row in rows}

    async def clear(
        self,
        connection: asyncpg.Connection,
        *,
        buckets: Sequence[tuple[str, str]],
    ) -> None:
        """Forget these buckets entirely.

        Called after a log-in that succeeded, which is the whole reason a
        legitimate user who fumbles their password four times is not one
        fumble away from being locked out for a quarter of an hour: getting it
        right puts them back at zero. It is also the only thing that keeps the
        address bucket from being a slow-motion lockout of every account whose
        owner is bad at typing.

        A DELETE and not a reset to zero. There is no row for zero attempts --
        `auth_rate_limits_attempts_positive` forbids one -- so absence IS zero,
        and the row's disappearance also does the sweep's job for the buckets
        that end in a success.

        Reports nothing. Whether a bucket existed to clear is a fact about how
        many times this account has recently failed, and the caller's next move
        is the same either way.
        """
        if not buckets:
            return

        await connection.execute(
            """
            DELETE FROM auth_rate_limits l
            USING unnest($1::text[], $2::text[]) AS bucket (scope, subject)
            WHERE l.scope = bucket.scope
              AND l.subject = bucket.subject
            """,
            [scope for scope, _ in buckets],
            [subject for _, subject in buckets],
        )

    async def delete_stale(
        self,
        connection: asyncpg.Connection,
        *,
        window: timedelta,
        batch: int,
    ) -> int:
        """Drop buckets whose window has aged out, up to `batch` of them.

        Space only. A stale bucket is already harmless -- `consume` rolls it
        over to 1 on the next attempt without consulting anything -- so this
        deletes nothing the limiter depends on and can fall arbitrarily far
        behind without changing a single decision.

        Bounded, for the reason `SessionRepository.delete_expired` is bounded:
        an unbounded DELETE on a table that has never been swept is one
        statement holding one transaction over every dead row in it, and the
        first time it matters is the first time it is slow.
        """
        # `ctid` rather than the primary key, which is two columns here and
        # would need a row-comparison IN list. The physical address is what a
        # DELETE resolves to anyway, and the subquery and the delete are one
        # statement, so there is no window in which a row could move between
        # them.
        status: str = await connection.execute(
            """
            DELETE FROM auth_rate_limits
            WHERE ctid IN (
                SELECT ctid
                FROM auth_rate_limits
                WHERE window_started_at <= now() - $1::interval
                LIMIT $2
            )
            """,
            window,
            batch,
        )

        return _deleted(status)


def _deleted(status: str) -> int:
    """The row count out of a DELETE's command tag.

    asyncpg ships no types for `execute`, so the tag is `Any` and an
    unannotated read of it would satisfy any return type at all. "DELETE 7"
    splits to its count; anything else is a tag from a statement this function
    was not meant to be handed, and int() raising is the right answer to that.
    """
    return int(status.split()[-1])
