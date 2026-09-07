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
from app.domain.events import URGENT_PRIORITY, DomainEventKind
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
from app.domain.subscribers import SubscriberEntity
from app.domain.tenancy import AuthorizedWorkspaceScope, WorkspaceScope
from app.repositories.activity import ActivityRepository
from app.repositories.notifications import NotificationRepository
from app.repositories.subscribers import SubscriberRepository
from app.services.events import record_issue_event


FIRST_MIN = 1
FIRST_MAX = 100

# The most watchers `list_subscribers` will return for one issue.
#
# A ceiling on a response rather than a page size, and there is deliberately no
# cursor behind it. An issue cannot have more watchers than the workspace has
# members, so this is a configuration-sized set rather than a product list that
# grows without bound -- the argument migration 020 makes for the table having
# no keyset index of its own.
#
# ponytail: a workspace with more than 500 members watching ONE issue would see
# a truncated list with nothing saying so. That workspace does not exist yet.
# When it does, this becomes a keyset page over (created_at, user_id), which
# `issue_subscribers_pkey` cannot serve -- so it arrives with an index, not
# just a cursor.
SUBSCRIBERS_MAX = 500

# The one foreign key on `issue_subscribers` a client can violate, matched by
# name so that any OTHER constraint failure stays an error instead of being
# reported to a client as something it can correct. See
# `ActivityService.subscribe` for why its sibling on `workspace_members` is
# deliberately absent from this.
_UNKNOWN_ISSUE_CONSTRAINT = "issue_subscribers_issue_fk"

ISSUE_NOT_FOUND = ValidationIssue(
    field="issueId",
    code="NOT_FOUND",
    message="Issue not found",
)

# Stateless, so one of each is all this process needs. They hold no
# connection, no pool and no scope -- every one of those arrives per call --
# which is what makes a module-level instance a shared function table rather
# than shared state.
_activity = ActivityRepository()
_notifications = NotificationRepository()
_subscribers = SubscriberRepository()


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

    The new assignee is also SUBSCRIBED, in the same transaction, and that
    ordering is the point: the subscription is written before the notification
    fans out, so the person who has just been handed the work is already a
    watcher of it and stays one after the next person is assigned. Being given
    an issue is the clearest possible statement that its future concerns you.

    A status move notifies too, and it is the one event that exists FOR the
    watchers -- an assignee can see the status on the issue they own, and a
    watcher asked to follow it precisely so they would not have to open it.
    `include_creator` is left false: an author who cares gets there by
    watching, and every issue's author hearing about every move is how an inbox
    becomes noise. The two notifications are independent, so a write that moves
    both the assignee and the state produces two items -- which is two things
    that happened, and collapsing them would mean choosing which one to hide.

    Three DOMAIN EVENTS are emitted alongside those notifications, and they are
    a different question with a different audience. A notification is addressed
    to one person's inbox; an event is a fact a workspace may want announced
    outside Vector, in a channel a whole team reads. So the vocabularies do not
    match and should not: `STATUS_CHANGED` reaches every watcher of every move,
    while `ISSUE_COMPLETED` is only the move that finishes something, and there
    is an urgency event with no inbox counterpart at all because "this became
    urgent" is exactly the kind of thing a channel is for and an inbox is not.

    They are emitted HERE for the reason the notifications are: this is the
    module that already knows what a write moved, and the alternative is
    fifteen call sites each deciding for themselves what is worth announcing --
    which is fifteen places for one of them to stop. Nothing about Slack is
    visible from this module or from the one it calls; see
    `app.services.events`.

    Whether a state move is a COMPLETION is decided inside the emitting
    statement rather than here, because `IssueSnapshot` carries state ids and
    not state types, and the join costs nothing this function does not already
    pay. A move to a state that is not a completed one simply writes no row.
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
        await auto_subscribe(
            connection,
            scope=scope,
            issue_id=issue_id,
            user_id=after.assignee_id,
        )

        await notify(
            connection,
            scope=scope,
            issue_id=issue_id,
            actor_id=actor_id,
            kind=NotificationKind.ASSIGNED,
        )

        await record_issue_event(
            connection,
            scope=scope,
            kind=DomainEventKind.ISSUE_ASSIGNED,
            issue_id=issue_id,
        )

    if before.workflow_state_id != after.workflow_state_id:
        await notify(
            connection,
            scope=scope,
            issue_id=issue_id,
            actor_id=actor_id,
            kind=NotificationKind.STATUS_CHANGED,
        )

        await record_issue_event(
            connection,
            scope=scope,
            kind=DomainEventKind.ISSUE_COMPLETED,
            issue_id=issue_id,
        )

    # A TRANSITION and not a state, which is what migration 018 says the event
    # is for: the message is worth sending when something BECOMES urgent, and a
    # channel that re-announced every urgent issue on every edit of it would be
    # the noise that vocabulary is short to avoid. `changes` has already
    # established the priority moved; this adds only that it moved TO the top of
    # the scale.
    if before.priority != after.priority and after.priority == URGENT_PRIORITY:
        await record_issue_event(
            connection,
            scope=scope,
            kind=DomainEventKind.ISSUE_PRIORITY_URGENT,
            issue_id=issue_id,
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


async def auto_subscribe(
    connection: asyncpg.Connection,
    *,
    scope: WorkspaceScope,
    issue_id: UUID,
    user_id: UUID,
) -> None:
    """Make somebody a watcher because of what they just did.

    Participation is the signal, and there are exactly two kinds of it today:
    commenting on an issue, and being handed it. Both are a person putting
    themselves into the issue's future, and neither is a moment at which anyone
    wants to be asked "would you also like to follow this?".

    Deliberately silent about whether it did anything. The caller is mid-write
    and has nothing to do with the answer -- somebody who already watches the
    issue is in exactly the state this call is asking for -- and returning one
    would invite a resolver to report it, which is how an internal record
    becomes part of a mutation's contract.

    Distinct from `ActivityService.subscribe` and not a private helper for it.
    This one takes the caller's connection, so the subscription commits with
    the comment or the assignment that caused it: a comment that exists with
    its author not following the thread, and a watcher of a change that was
    rolled back, are both states this shape makes unreachable. The service
    method is the deliberate, standalone act with a transaction of its own.

    `SubscriberRepository.subscribe` absorbs the duplicate rather than raising,
    which is what makes this callable from inside somebody else's transaction
    at all: in PostgreSQL a constraint violation aborts the whole transaction,
    so "catch it and carry on" is not available here.

    A user who is not a member of the workspace violates
    `issue_subscribers_user_fk` and that violation propagates untranslated. It
    is not client input: both callers pass an id the database has already
    matched against `workspace_members` -- a comment author through
    `comments_author_fk`, an assignee through `issues_assignee_fk` -- so a
    failure here means one of those constraints did not hold, which is a defect
    and not something for a client to correct.
    """
    await _subscribers.subscribe(
        connection,
        scope=scope,
        issue_id=issue_id,
        user_id=user_id,
    )


class ActivityService:
    """One issue's history, one person's inbox, and who is watching what.

    Mostly reads. The four that write -- `mark_read`, `mark_all_read`,
    `subscribe` and `unsubscribe` -- are all deliberate acts a person performs
    on their OWN row, each with a transaction of its own, and none of them
    records that something happened to an issue. That distinction is the one
    `app/graphql/context.py` describes: history and inbox rows caused by a
    change are written by the service that causes them, on that service's
    connection, through the module-level functions above -- never through this
    object, which would be a way to record an event that had not happened yet.

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
        subscribers: SubscriberRepository,
    ):
        self._pool = pool
        self._repository = repository
        self._notifications = notifications

        # Watching is the third table this service reads, and it belongs with
        # the other two rather than in a service of its own: a subscription is
        # a standing answer to the question the notification half asks on every
        # write -- "whose problem is this" -- and the two are read together by
        # every screen that shows an issue.
        self._subscribers = subscribers

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

    async def subscribe(
        self,
        *,
        scope: AuthorizedWorkspaceScope,
        issue_id: UUID,
    ) -> bool:
        """Watch one issue; True if this call is what started it.

        There is no `user_id` parameter, and that absence is the design, for
        the reason `list_notifications` gives: the watcher is `scope.user_id`,
        which came out of `workspace_members`. A method that accepted one would
        be a way to sign somebody else up for an issue's notifications, and no
        amount of checking at the call site would make it safe -- there would
        simply be one call site that forgot.

        Idempotent. Watching an issue twice succeeds, answers False and leaves
        the original "watching since" where it was, so a retry or two tabs
        cannot move it.

        Two situations produce one answer: the issue is in another workspace,
        and the issue does not exist. Both are "Issue not found", because a
        distinguishable answer would tell a caller holding a guessed id that
        the issue is real and simply not theirs. Only that one constraint is
        translated -- `issue_subscribers_user_fk` cannot fire, since an
        `AuthorizedWorkspaceScope` is built from a membership row, so a
        violation of it means that row went away mid-request and is a failure
        to surface rather than advice for a client.
        """
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                try:
                    return await self._subscribers.subscribe(
                        connection,
                        scope=scope,
                        issue_id=issue_id,
                        user_id=scope.user_id,
                    )
                except asyncpg.ForeignKeyViolationError as exc:
                    if exc.constraint_name != _UNKNOWN_ISSUE_CONSTRAINT:
                        raise

                    raise ValidationError([ISSUE_NOT_FOUND]) from None

    async def unsubscribe(
        self,
        *,
        scope: AuthorizedWorkspaceScope,
        issue_id: UUID,
    ) -> bool:
        """Stop watching one issue; True if a row went.

        Unwatching an issue nobody was watching is a successful no-op rather
        than an error, and so is unwatching one in another workspace: the
        caller asked for a state, the state holds, and reporting a failure
        would make a retry after a dropped response look like a different
        outcome from the first attempt. It also keeps existence unobservable,
        which the subscribe path cannot -- that one has to say when it did
        nothing.

        A DELETE and not a tombstone. Commenting on the issue again will
        re-subscribe the same person, which is the product rule migration 020
        argues for: commenting is asking to be part of the conversation.
        """
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                return await self._subscribers.unsubscribe(
                    connection,
                    scope=scope,
                    issue_id=issue_id,
                    user_id=scope.user_id,
                )

    async def list_subscribers(
        self,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
    ) -> list[SubscriberEntity]:
        """Everyone watching one issue, oldest subscription first.

        A plain `WorkspaceScope`, unlike the inbox methods above, because a
        watcher list is not addressed to anyone: it is as visible as the issue,
        and the issue was resolved under that scope. It is the same judgement
        `list_for_issue` makes about a history.

        A single SELECT needs no explicit write transaction, so this acquires a
        connection without opening one.

        An issue in another workspace answers an empty list, exactly as an
        issue nobody watches does.
        """
        async with self._pool.acquire() as connection:
            return await self._subscribers.list_for_issue(
                connection,
                scope=scope,
                issue_id=issue_id,
                limit=SUBSCRIBERS_MAX,
            )

    async def is_subscribed(
        self,
        *,
        scope: AuthorizedWorkspaceScope,
        issue_id: UUID,
    ) -> bool:
        """Whether the caller is watching one issue.

        Answers about `scope.user_id` and nothing else, for the reason
        `subscribe` gives about having no `user_id` parameter -- with a second
        one here: a method that answered about an arbitrary user would report
        whether a named person is watching a named issue, which is a fact about
        them rather than about the issue.
        """
        async with self._pool.acquire() as connection:
            return await self._subscribers.is_subscribed(
                connection,
                scope=scope,
                issue_id=issue_id,
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
