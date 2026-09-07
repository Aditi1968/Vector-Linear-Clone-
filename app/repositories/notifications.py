from datetime import datetime
from uuid import UUID

import asyncpg

from app.domain.notifications import NotificationEntity, NotificationKind
from app.domain.tenancy import WorkspaceScope


_NOTIFICATION_COLUMNS = """
                id,
                user_id,
                actor_id,
                issue_id,
                kind,
                read_at,
                created_at
"""


# One person's inbox, as a template with two holes.
#
# Four statement texts are built from it rather than one text with tolerant
# predicates, for the reason `app/repositories/relations.py` sets out at
# `_RELATIONS_TEMPLATE`: a parameter a statement never mentions has no
# inferable type and fails to parse, and the obvious repairs -- `($3 IS NULL
# OR ...)` for the cursor, `($n OR read_at IS NULL)` for the filter -- parse
# and then cost the index. The second one is the expensive mistake here: an OR
# over a parameter cannot match the PARTIAL index
# `notifications_workspace_user_unread_idx`, so the unread listing and the
# badge count would both degrade into a scan of the whole inbox, which is the
# one thing that index exists to prevent.
#
# Interpolated from module-level literals only. Every value below is $n.
_INBOX_TEMPLATE = f"""
    SELECT
{_NOTIFICATION_COLUMNS}
    FROM notifications
    WHERE workspace_id = $1 AND user_id = $2
        {{unread}}
        {{cursor}}
    ORDER BY created_at DESC, id DESC
    LIMIT ${{limit}}
"""

_UNREAD_PREDICATE = "AND read_at IS NULL"
_CURSOR_PREDICATE = "AND (created_at, id) < ($3, $4)"

# Keyed by (unread_only, has_cursor). A dict rather than four `if`s at the
# call site: the four texts are one decision made once, and the lookup below
# cannot reach a combination that was never built.
_INBOX_STATEMENTS = {
    (False, False): _INBOX_TEMPLATE.format(unread="", cursor="", limit=3),
    (True, False): _INBOX_TEMPLATE.format(unread=_UNREAD_PREDICATE, cursor="", limit=3),
    (False, True): _INBOX_TEMPLATE.format(unread="", cursor=_CURSOR_PREDICATE, limit=5),
    (True, True): _INBOX_TEMPLATE.format(
        unread=_UNREAD_PREDICATE, cursor=_CURSOR_PREDICATE, limit=5
    ),
}


class NotificationRepository:
    """SQL access for `notifications`.

    The repository receives a connection from the service layer. It never
    acquires connections, never touches the pool, and never owns a
    transaction. `asyncpg.Record` never escapes this class.

    Every statement here is scoped to one workspace AND to one user, and both
    arrive as required keyword arguments. That pairing is the whole security
    property of an inbox: `workspace_id` alone would let a member of a
    workspace read every colleague's notifications, and `user_id` alone would
    let a member of one workspace read their own notifications from another.
    Neither is a filter applied to rows already fetched; both are equalities
    in the WHERE clause, so a row belonging to somebody else is never read
    into this process at all.
    """

    async def notify_about_issue(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
        actor_id: UUID | None,
        kind: NotificationKind,
        include_creator: bool,
    ) -> None:
        """File one notification per interested party, minus the actor.

        Recipients are derived from the issue's own row and from
        `issue_subscribers` by the server, rather than read into Python and
        written back. Three things follow, and all three are the reason for the
        shape:

        * it is one statement, so there is no window in which the assignee
          changes -- or somebody unwatches -- between the read and the write;
        * a service cannot pass a recipient it invented, because there is no
          parameter for one;
        * the actor exclusion (`IS DISTINCT FROM $2`) is evaluated against the
          same rows, so "do not notify me about my own action" cannot be
          skipped by a caller that forgot -- and cannot be violated at all,
          since `notifications_actor_is_not_recipient` refuses the row.

        The subscriber arm is what migration 020 adds, and it is a third UNION
        arm rather than a second method. Every event that reaches an inbox
        reaches a watcher's inbox: that is what watching means, so a caller
        that could choose to skip subscribers would be a caller who could
        silently un-implement the feature. There is no flag for it.

        `include_creator` stays a flag, because the creator genuinely differs
        by kind -- an author cares about a comment on their issue and not about
        every status move -- and the two statements would otherwise differ by
        one row and have to be kept identical in every other respect.

        The JOIN onto `workspace_members` is not decoration. `issues.creator_id
        references users (id)` -- migration 006 argues for that at length --
        so an issue's author may no longer be a member of its workspace, and a
        notification for a non-member is both a leak and a violation of
        `notifications_user_fk`. Joining rather than checking means the
        non-member is dropped by the same statement that finds them. A
        subscriber cannot be a non-member -- `issue_subscribers_user_fk` is
        composite through the workspace -- and passes through the same join
        anyway, so nothing here depends on that constraint holding to be
        correct.

        `member.removed_at IS NULL` is the half of that join the constraint no
        longer backs. Since 026 a departed member keeps their row, so
        `notifications_user_fk` is satisfied by somebody who left and this
        predicate is the only thing standing between them and an inbox they
        cannot open -- a former colleague reading the titles of new issues by
        email is exactly the leak the join was written to prevent, and it would
        arrive silently. `MembershipService.remove_member` deletes their
        subscriptions and their existing notifications on the way out; this is
        what stops new ones being written afterwards, for the assignee and
        creator arms that no deletion can reach.

        DISTINCT because the assignee, the creator and a subscriber are
        frequently the same person, and one event is one item in one inbox.

        The casts in the select list are load-bearing, not decoration. In an
        `INSERT ... SELECT`, PostgreSQL resolves an untyped parameter in the
        select list to `text` before the INSERT's target columns are consulted
        -- so `$2` would be `text` there and `uuid` in the `IS DISTINCT FROM`
        below, and the statement fails to PARSE with "inconsistent types
        deduced for parameter $2". Naming the type once is what makes both
        uses agree.
        """
        await connection.execute(
            """
            WITH candidate AS (
                SELECT recipient
                FROM issues
                CROSS JOIN LATERAL (
                    VALUES
                        (issues.assignee_id),
                        (CASE WHEN $5 THEN issues.creator_id END)
                ) AS from_issue (recipient)
                WHERE issues.workspace_id = $1
                    AND issues.id = $3

                UNION ALL

                SELECT subscriber.user_id
                FROM issue_subscribers AS subscriber
                WHERE subscriber.workspace_id = $1
                    AND subscriber.issue_id = $3
            )
            INSERT INTO notifications (
                workspace_id,
                user_id,
                actor_id,
                issue_id,
                kind
            )
            SELECT DISTINCT
                $1::UUID, member.user_id, $2::UUID, $3::UUID, $4::TEXT
            FROM candidate
            JOIN workspace_members AS member
                ON member.workspace_id = $1
                AND member.user_id = candidate.recipient
                AND member.removed_at IS NULL
            WHERE candidate.recipient IS DISTINCT FROM $2
            """,
            scope.workspace_id,
            actor_id,
            issue_id,
            kind.value,
            include_creator,
        )

    async def list_for_user(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        user_id: UUID,
        unread_only: bool,
        limit: int,
        after_created_at: datetime | None,
        after_id: UUID | None,
    ) -> list[NotificationEntity]:
        """Keyset page of one user's notifications here, newest first.

        `limit` is expected to already be `first + 1` so the caller can detect
        a following page. No OFFSET: the cursor is a row-value comparison over
        (created_at, id), which is the ordering both indexes carry.

        The two equalities are ANDed with the cursor rather than folded into
        it. Widening the row-value comparison to include workspace_id or
        user_id would put them into the ORDER BY, which is how a page walk
        leaves one inbox and continues into the next one that sorts.
        """
        has_cursor = after_created_at is not None and after_id is not None
        statement = _INBOX_STATEMENTS[(unread_only, has_cursor)]

        if has_cursor:
            rows = await connection.fetch(
                statement,
                scope.workspace_id,
                user_id,
                after_created_at,
                after_id,
                limit,
            )
        else:
            rows = await connection.fetch(
                statement,
                scope.workspace_id,
                user_id,
                limit,
            )

        return [self._to_entity(row) for row in rows]

    async def count_unread(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        user_id: UUID,
    ) -> int:
        """How many unread notifications this user has in this workspace.

        The predicate is written exactly as
        `notifications_workspace_user_unread_idx` declares it, so the count
        reads an index whose size tracks the unread rows rather than the
        table -- which is what keeps a badge cheap for an account with a
        five-year read backlog.

        `count(*)` never returns NULL, but the COALESCE is not there for that:
        `fetchval` returns None if the statement somehow matches no row at
        all, and a badge is an int in the schema.
        """
        count = await connection.fetchval(
            """
            SELECT count(*)
            FROM notifications
            WHERE workspace_id = $1 AND user_id = $2 AND read_at IS NULL
            """,
            scope.workspace_id,
            user_id,
        )

        return int(count or 0)

    async def mark_read(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        user_id: UUID,
        notification_id: UUID,
    ) -> NotificationEntity | None:
        """Mark one of this user's own notifications read, idempotently.

        `read_at = COALESCE(read_at, now())` is what makes a second call a
        no-op rather than a rewrite: the timestamp keeps meaning "when this
        was first read" instead of "when it was last marked", so a client that
        retries a request -- or two tabs doing it at once -- cannot move it.

        The owner is an equality in the WHERE clause, never a check applied to
        a row already read. A notification belonging to somebody else, one in
        another workspace, and an id that exists nowhere all match no row and
        answer None -- one answer, reached without this process ever loading a
        row it was not entitled to see. Distinguishing them would confirm that
        an id names a real notification somebody really received.
        """
        row = await connection.fetchrow(
            f"""
            UPDATE notifications
            SET read_at = COALESCE(read_at, now())
            WHERE workspace_id = $1 AND user_id = $2 AND id = $3
            RETURNING
{_NOTIFICATION_COLUMNS}
            """,
            scope.workspace_id,
            user_id,
            notification_id,
        )

        if row is None:
            return None

        return self._to_entity(row)

    async def mark_all_read(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        user_id: UUID,
    ) -> int:
        """Mark every unread notification of this user's read; count the rows.

        `read_at IS NULL` in the predicate rather than COALESCE in the SET,
        which is the same idempotence reached the other way and does less
        work: already-read rows are not matched, so they are not rewritten and
        not counted. The count is therefore "how many were unread", which is
        the number a client needs to zero its badge -- and running it twice
        answers 0 the second time rather than the same number again.
        """
        rows = await connection.fetch(
            """
            UPDATE notifications
            SET read_at = now()
            WHERE workspace_id = $1 AND user_id = $2 AND read_at IS NULL
            RETURNING id
            """,
            scope.workspace_id,
            user_id,
        )

        return len(rows)

    @staticmethod
    def _to_entity(row: asyncpg.Record) -> NotificationEntity:
        return NotificationEntity(
            id=row["id"],
            user_id=row["user_id"],
            actor_id=row["actor_id"],
            issue_id=row["issue_id"],
            kind=NotificationKind(row["kind"]),
            read_at=row["read_at"],
            created_at=row["created_at"],
        )
