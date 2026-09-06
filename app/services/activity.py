"""Recording what happened, and deciding whose problem it is.

Two halves with two shapes, and the split is deliberate.

The WRITE half is the module-level functions. They take a caller's connection
instead of acquiring one, because an activity row that is not written in the
same transaction as the change it describes is a lie waiting to happen: the
change rolls back, the history keeps the row, and the timeline reports an edit
nobody made. Passing the connection is how the layering rule -- services own
transactions, repositories receive connections -- delivers atomicity here.

They are functions rather than a collaborator object every writing service has
to be constructed with. The alternative was a required constructor argument on
`IssueService`, `CommentService`, `LabelService` and `RelationService`, which
is fifteen call sites and four `__init__` signatures for an object that holds
no pool, no configuration and no state -- there is nothing about it a caller
could get right or wrong. `app/graphql/viewer.py` is the same judgement made
for the same reason: one module, not one copy per feature.

The READ half is `ActivityService`, which owns a pool like every other
service, because a query needs a connection of its own.
"""

from collections.abc import Sequence
from uuid import UUID

import asyncpg

from app.domain.activity import (
    ActivityEntity,
    ActivityKind,
    ActivityPage,
    IssueSnapshot,
    changes,
)
from app.domain.errors import ValidationError, ValidationIssue
from app.domain.notifications import (
    NotificationEntity,
    NotificationKind,
    NotificationPage,
)
from app.domain.pagination import (
    InvalidCursorError,
    KeysetCursor,
    decode_keyset_cursor,
    encode_keyset_cursor,
)
from app.domain.tenancy import AuthorizedWorkspaceScope, WorkspaceScope
from app.repositories.activity import ActivityRepository
from app.repositories.notifications import NotificationRepository


FIRST_MIN = 1
FIRST_MAX = 100

# Stateless, so one of each is all this process needs. They hold no
# connection, no pool and no scope -- every one of those arrives per call --
# which is what makes a module-level instance a shared function table rather
# than shared state.
_activity = ActivityRepository()
_notifications = NotificationRepository()


async def record(
    connection: asyncpg.Connection,
    *,
    scope: WorkspaceScope,
    issue_id: UUID,
    actor_id: UUID | None,
    kind: ActivityKind,
    from_value: str | None = None,
    to_value: str | None = None,
) -> None:
    """Append one event to one issue's history, on the caller's connection.

    Call this INSIDE the transaction that performs the change, never after
    it. The whole guarantee is that the two commit together.
    """
    await _activity.record(
        connection,
        scope=scope,
        issue_id=issue_id,
        actor_id=actor_id,
        kind=kind,
        from_value=from_value,
        to_value=to_value,
    )


async def record_changes(
    connection: asyncpg.Connection,
    *,
    scope: WorkspaceScope,
    issue_id: UUID,
    actor_id: UUID | None,
    before: IssueSnapshot,
    after: IssueSnapshot,
) -> None:
    """Record every field one write actually moved, and notify a new assignee.

    One row per field rather than one row carrying six columns of before and
    after, because the timeline renders one sentence per change and a client
    that had to unpack which of six pairs differed would be reimplementing
    `app.domain.activity.changes` in TypeScript.

    A loop of small inserts rather than one multi-row statement: an update
    touches one or two fields in practice, they are already inside the
    caller's transaction, and a batched insert would buy one round trip in
    exchange for a statement whose parameter list is built at runtime.

    The assignment notification is decided here rather than by the caller,
    because "who is told about this" is this module's question. It fires only
    when the assignee MOVED -- `changes` has already dropped a re-assignment
    to the same person -- and only when there is somebody to tell, which
    unassigning is not.
    """
    moved = changes(before, after)

    for kind, from_value, to_value in moved:
        await record(
            connection,
            scope=scope,
            issue_id=issue_id,
            actor_id=actor_id,
            kind=kind,
            from_value=from_value,
            to_value=to_value,
        )

    if after.assignee_id is not None and before.assignee_id != after.assignee_id:
        await notify(
            connection,
            scope=scope,
            issue_id=issue_id,
            actor_id=actor_id,
            kind=NotificationKind.ASSIGNED,
        )


async def notify(
    connection: asyncpg.Connection,
    *,
    scope: WorkspaceScope,
    issue_id: UUID,
    actor_id: UUID | None,
    kind: NotificationKind,
    include_creator: bool = False,
) -> None:
    """Put one item in the inbox of everyone this issue's state concerns.

    Never in the actor's own. That rule is enforced three times over and
    deliberately so: here by intent, in the statement by
    `IS DISTINCT FROM`, and in the schema by
    `notifications_actor_is_not_recipient`. It is the rule most likely to be
    quietly lost when a fourth caller appears, and its failure mode is silent
    -- an inbox that reads back everything its owner just did.

    Recipients are the issue's assignee, plus its creator when
    `include_creator` says the event is one an author cares about. Neither is
    named by the caller; both are read from the issue by the statement itself.

    Deliberately no return value and no error for "nobody to notify". An
    unassigned issue nobody authored produces no rows, which is an ordinary
    outcome and not a failure of the write that caused it.
    """
    await _notifications.notify_about_issue(
        connection,
        scope=scope,
        issue_id=issue_id,
        actor_id=actor_id,
        kind=kind,
        include_creator=include_creator,
    )


class ActivityService:
    """Reads of one issue's history and of one person's inbox.

    Every notification method takes an `AuthorizedWorkspaceScope` rather than
    a `WorkspaceScope`, and that is the authorization, not a formality. Only
    `MembershipService.authorized_scope_for_slug` can build one, only from a
    row in `workspace_members`, and its `user_id` is the id the DATABASE
    matched for the authenticated caller. So the recipient filter on every
    statement below comes from a membership lookup rather than from anything a
    client sent -- there is no parameter through which a caller could name
    somebody else's inbox, in this workspace or any other.

    `list_for_issue` takes a plain `WorkspaceScope`, because an issue's
    history is not addressed to anyone: it is as visible as the issue, and the
    issue was resolved under that scope.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        repository: ActivityRepository,
        notifications: NotificationRepository,
    ):
        self._pool = pool
        self._repository = repository
        self._notifications = notifications

    async def list_for_issue(
        self,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
        first: int,
        after: str | None,
    ) -> ActivityPage:
        """Forward keyset page of one issue's history, newest first.

        A single SELECT needs no explicit write transaction, so this acquires
        a connection without opening one.

        An issue in another workspace is not refused and is not reported: it
        returns an empty page, exactly as an issue nothing has happened to
        does. That equivalence is the isolation property -- any distinguishable
        answer would tell a caller holding a guessed id that the issue is real
        and simply not theirs.
        """
        cursor = self._validate_page(first=first, after=after)

        async with self._pool.acquire() as connection:
            # One extra row tells us whether a further page exists.
            rows = await self._repository.list_for_issue(
                connection,
                scope=scope,
                issue_id=issue_id,
                limit=first + 1,
                after_created_at=cursor.created_at if cursor is not None else None,
                after_id=cursor.id if cursor is not None else None,
            )

        nodes, has_next_page = self._page(rows, first)

        return ActivityPage(
            nodes=nodes,
            has_next_page=has_next_page,
            end_cursor=self._end_cursor(nodes),
        )

    async def list_notifications(
        self,
        *,
        scope: AuthorizedWorkspaceScope,
        unread_only: bool,
        first: int,
        after: str | None,
    ) -> NotificationPage:
        """Forward keyset page of the caller's own inbox here, newest first.

        There is no `user_id` parameter, and that absence is the design: the
        recipient is `scope.user_id`, which came out of `workspace_members`.
        A method that accepted one would be a way to read another person's
        inbox by sending their id, and no amount of checking at the call site
        would make it safe -- there would simply be one call site that forgot.
        """
        cursor = self._validate_page(first=first, after=after)

        async with self._pool.acquire() as connection:
            rows = await self._notifications.list_for_user(
                connection,
                scope=scope,
                user_id=scope.user_id,
                unread_only=unread_only,
                limit=first + 1,
                after_created_at=cursor.created_at if cursor is not None else None,
                after_id=cursor.id if cursor is not None else None,
            )

        nodes, has_next_page = self._page(rows, first)

        return NotificationPage(
            nodes=nodes,
            has_next_page=has_next_page,
            end_cursor=self._end_cursor(nodes),
        )

    async def unread_count(self, *, scope: AuthorizedWorkspaceScope) -> int:
        """How many unread items the caller has in this workspace."""
        async with self._pool.acquire() as connection:
            return await self._notifications.count_unread(
                connection,
                scope=scope,
                user_id=scope.user_id,
            )

    async def mark_read(
        self,
        *,
        scope: AuthorizedWorkspaceScope,
        notification_id: UUID,
    ) -> NotificationEntity:
        """Mark one of the caller's own notifications read.

        Idempotent: marking an already-read item succeeds and leaves the
        original `read_at` where it was. A client that retries, or two tabs
        that both fire, therefore agree about when it was read.

        Three situations produce one answer: the notification is in another
        workspace, it belongs to somebody else, and it never existed. All
        three are "Notification does not exist", because the two the caller is
        not entitled to must not be distinguishable from the one that is
        ordinary -- "that is not yours" confirms that the id names a real item
        somebody really received.
        """
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                entity = await self._notifications.mark_read(
                    connection,
                    scope=scope,
                    user_id=scope.user_id,
                    notification_id=notification_id,
                )

        if entity is None:
            raise ValidationError(
                [
                    ValidationIssue(
                        field="id",
                        code="NOT_FOUND",
                        message="Notification does not exist",
                    )
                ]
            )

        return entity

    async def mark_all_read(self, *, scope: AuthorizedWorkspaceScope) -> int:
        """Mark every unread item of the caller's read; return how many moved.

        Zero is a success, not a miss. An empty inbox is the state the caller
        asked for, and reporting it as an error would make "clear my
        notifications" fail for the person who has none.
        """
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                return await self._notifications.mark_all_read(
                    connection,
                    scope=scope,
                    user_id=scope.user_id,
                )

    @staticmethod
    def _page[T](rows: list[T], first: int) -> tuple[list[T], bool]:
        """Split the `first + 1` rows fetched into a page and the hint."""
        return rows[:first], len(rows) > first

    @staticmethod
    def _end_cursor(nodes: Sequence[ActivityEntity | NotificationEntity]) -> str | None:
        """The cursor for the last RETURNED node, never the extra row.

        Both pages are keyed on `(created_at, id)`, so both mint the same
        cursor with the codec's tableless name -- the payload is a timestamp
        and a uuid, and a second wire format differing only in the name of the
        function that writes it would be a second thing to keep in step.
        """
        if not nodes:
            return None

        last = nodes[-1]

        return encode_keyset_cursor(last.created_at, last.id)

    @staticmethod
    def _validate_page(*, first: int, after: str | None) -> KeysetCursor | None:
        """Validate pagination arguments, returning the decoded cursor.

        `first` is never silently clamped, and an invalid cursor is an
        expected input error rather than a parser exception.
        """
        issues: list[ValidationIssue] = []
        cursor: KeysetCursor | None = None

        if first < FIRST_MIN or first > FIRST_MAX:
            issues.append(
                ValidationIssue(
                    field="first",
                    code="OUT_OF_RANGE",
                    message=f"first must be between {FIRST_MIN} and {FIRST_MAX}",
                )
            )

        if after is not None:
            try:
                cursor = decode_keyset_cursor(after)
            except InvalidCursorError:
                issues.append(
                    ValidationIssue(
                        field="after",
                        code="INVALID_CURSOR",
                        message="Cursor is invalid",
                    )
                )

        if issues:
            raise ValidationError(issues)

        return cursor
