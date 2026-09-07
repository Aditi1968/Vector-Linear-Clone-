"""What one person still has to look at.

Pure application code -- no Strawberry, FastAPI, asyncpg or PostgreSQL.

A notification is not an event and not a history row. It is addressed to one
user, it has a read state that belongs to that user alone, and the same event
produces one row per recipient or none at all. See
migrations/012_activity_notifications.sql for why it is a separate table from
`issue_activity` rather than a column on it.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class NotificationKind(StrEnum):
    """The four events worth interrupting somebody for.

    Deliberately far shorter than `ActivityKind`. Everything that happens to
    an issue belongs in its history; almost none of it belongs in anybody's
    inbox, and a vocabulary that mirrored the history's twelve would produce
    an inbox nobody reads -- which is the same as no inbox, arrived at
    expensively.

    STATUS_CHANGED was earned by subscribers rather than assumed with them.
    Until migration 020 the recipients of an event were the issue's assignee
    and, for some kinds, its creator -- both of whom can see the status on the
    issue they already own. A watcher cannot: they asked to follow an issue
    precisely so they would not have to open it, and the move is the thing
    they are following. The kind lands with the table that makes it useful.

    Not STATE_CHANGED, which is what `ActivityKind` calls the same event. The
    two vocabularies are separate on purpose and the inbox's names are the ones
    a client renders into a sentence a person reads; "status" is the word the
    product uses on screen.

    There is no MENTIONED. Mentions do not exist in this product: nothing
    parses a comment body for user references and no table records one.
    Adding the kind now would mean inventing the concept, so the kind lands
    with the feature.

    The application's copy of `notifications_kind_known` -- declared in
    migration 012 and widened by 020 -- for the reason `ActivityKind` gives.
    """

    ASSIGNED = "assigned"
    COMMENTED = "commented"
    BLOCKED = "blocked"
    STATUS_CHANGED = "status_changed"


@dataclass(frozen=True, slots=True)
class NotificationEntity:
    """One item in one person's inbox.

    `read_at` is the whole read state: None is unread, and a timestamp is when
    it was first read. There is no boolean beside it, here or in the table,
    because two spellings of one fact can disagree.

    `user_id` is carried even though every read is already filtered to the
    caller. It is what a test asserts against, and what makes a row that
    somehow reached the wrong reader visible rather than silent.
    """

    id: UUID
    user_id: UUID
    actor_id: UUID | None
    issue_id: UUID
    kind: NotificationKind
    read_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class NotificationPage:
    nodes: list[NotificationEntity]
    has_next_page: bool
    end_cursor: str | None
