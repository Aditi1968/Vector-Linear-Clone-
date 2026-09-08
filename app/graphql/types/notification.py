"""Transport types for one person's inbox."""

import enum
from datetime import datetime
from uuid import UUID

import strawberry
from strawberry.types import Info

from app.domain.notifications import NotificationEntity, NotificationPage
from app.domain.tenancy import WorkspaceScope
from app.graphql.types.errors import ValidationErrorType
from app.graphql.types.pagination import PageInfo
from app.graphql.types.relations import IssueSummaryType


# Page size for `notifications` when a document does not say.
#
# Fifty, matching `Query.issues`, because this is a ROOT field: nothing
# multiplies it, so `app.graphql.limits` charges it once at its declared
# default rather than once per row of an enclosing page. The nested defaults
# elsewhere are small for that reason and this one has no reason to be.
DEFAULT_NOTIFICATION_FIRST = 50


@strawberry.enum(name="NotificationKind")
class NotificationKindEnum(enum.Enum):
    """The five events that reach an inbox.

    Declared here rather than by decorating the domain enum, for the reason
    `IssueActivityKind` gives. There is no MENTIONED: mentions do not exist in
    this product, and publishing the name would promise a feature.

    STATUS_CHANGED arrived with subscribers in migration 020: it is the event a
    watcher is watching for, and until there were watchers its only recipients
    would have been people who can already see the status on an issue they own.

    DUE_SOON arrived with migration 029, and it is the only one of the five that
    nobody DID: the other four are somebody's action reaching the people it
    concerns, and this one is a date arriving. So `actorId` is null on every one
    of them, and a client rendering "X did Y" needs a different sentence for this
    kind.
    """

    ASSIGNED = "assigned"
    COMMENTED = "commented"
    BLOCKED = "blocked"
    STATUS_CHANGED = "status_changed"
    DUE_SOON = "due_soon"


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

    # Carried, not exposed. The scope the root field AUTHORIZED, handed down
    # so `issue` below reads in the workspace this notification was read from
    # -- never from the row, and never from anything ambient on the context.
    scope: strawberry.Private[WorkspaceScope]

    @strawberry.field(
        description=(
            "The issue this is about, or null where it is no longer visible "
            "-- an archived issue, most often. Cheap to select: one query per "
            "page of notifications rather than one per row."
        )
    )
    async def issue(self, info: Info) -> IssueSummaryType | None:
        """What the notification is about, named rather than described.

        `issueId` alone lets an inbox row LINK to an issue without being able
        to say which one, so the screen ends up describing the event instead
        of the work. This is the summary type `Issue.parent` and
        `Issue.children` already return, for the reason that type exists: it
        has no edges, so nothing reachable from an inbox leads back to an
        issue and out again into another page of rows.

        Batched through the request's loader, so a page of twenty-five
        notifications costs one statement rather than twenty-five.

        Null covers an archived issue and -- unreachably through the schema,
        since `notifications_issue_fk` is composite over the workspace -- an
        issue this scope cannot see. The second is a floor rather than an
        expected answer: if a row ever did disagree with its tenant, this
        resolves to null instead of handing back an issue from outside the
        workspace that asked.
        """
        entity = await info.context.issue_summaries.load(
            (self.scope.workspace_id, self.issue_id)
        )

        if entity is None:
            return None

        return IssueSummaryType.from_entity(entity)

    @classmethod
    def from_entity(
        cls,
        entity: NotificationEntity,
        scope: WorkspaceScope,
    ) -> "NotificationType":
        return cls(
            scope=scope,
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
    def from_domain(
        cls,
        page: NotificationPage,
        scope: WorkspaceScope,
    ) -> "NotificationConnection":
        return cls(
            nodes=[
                NotificationType.from_entity(entity, scope) for entity in page.nodes
            ],
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
