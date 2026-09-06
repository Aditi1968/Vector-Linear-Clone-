"""Transport types for one person's inbox."""

import enum
from datetime import datetime
from uuid import UUID

import strawberry

from app.domain.notifications import NotificationEntity, NotificationPage
from app.graphql.types.errors import ValidationErrorType
from app.graphql.types.pagination import PageInfo


# Page size for `notifications` when a document does not say.
#
# Fifty, matching `Query.issues`, because this is a ROOT field: nothing
# multiplies it, so `app.graphql.limits` charges it once at its declared
# default rather than once per row of an enclosing page. The nested defaults
# elsewhere are small for that reason and this one has no reason to be.
DEFAULT_NOTIFICATION_FIRST = 50


@strawberry.enum(name="NotificationKind")
class NotificationKindEnum(enum.Enum):
    """The three events that reach an inbox.

    Declared here rather than by decorating the domain enum, for the reason
    `IssueActivityKind` gives. There is no MENTIONED: mentions do not exist in
    this product, and publishing the name would promise a feature.
    """

    ASSIGNED = "assigned"
    COMMENTED = "commented"
    BLOCKED = "blocked"


@strawberry.type(name="Notification")
class NotificationType:
    """One item in the viewer's inbox.

    Every field here describes something the viewer is entitled to see: they
    are the recipient, the issue is one in a workspace they belong to, and the
    actor is whoever did it. There is no `userId`, because there is only ever
    one answer -- the viewer -- and a field that always says the same thing
    invites a client to believe it could say something else.

    `read_at` is the entire read state. Null is unread; a timestamp is when it
    was first read and does not move when it is marked again.
    """

    id: UUID
    actor_id: UUID | None
    issue_id: UUID
    kind: NotificationKindEnum
    read_at: datetime | None
    created_at: datetime

    @classmethod
    def from_entity(cls, entity: NotificationEntity) -> "NotificationType":
        return cls(
            id=entity.id,
            actor_id=entity.actor_id,
            issue_id=entity.issue_id,
            kind=NotificationKindEnum(entity.kind.value),
            read_at=entity.read_at,
            created_at=entity.created_at,
        )


@strawberry.type
class NotificationConnection:
    nodes: list[NotificationType]
    page_info: PageInfo

    @classmethod
    def from_domain(cls, page: NotificationPage) -> "NotificationConnection":
        return cls(
            nodes=[NotificationType.from_entity(entity) for entity in page.nodes],
            page_info=PageInfo(
                has_next_page=page.has_next_page,
                end_cursor=page.end_cursor,
            ),
        )


@strawberry.type
class NotificationMarkReadPayload:
    """The notification as it now is, so a client can render the read state.

    The whole object rather than a boolean, because the answer includes WHEN
    it was read -- and marking an already-read item returns the original
    instant rather than now, which is the visible half of the idempotence.

    Null when nothing was marked, which is also the answer for a notification
    belonging to somebody else: those two must not be distinguishable.
    """

    notification: NotificationType | None
    errors: list[ValidationErrorType]


@strawberry.type
class NotificationMarkAllReadPayload:
    """How many items moved from unread to read.

    A count rather than the rows: a client clearing its inbox needs to zero a
    badge, not to re-render items it is about to stop showing. Zero is a
    success -- an empty inbox is the state the caller asked for.
    """

    marked_count: int
    errors: list[ValidationErrorType]
