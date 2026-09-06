import strawberry
from strawberry.types import Info

from app.domain.errors import ValidationError
from app.graphql.inputs.notification import (
    NotificationMarkAllReadInput,
    NotificationMarkReadInput,
)
from app.graphql.types.errors import ValidationErrorType
from app.graphql.types.notification import (
    NotificationMarkAllReadPayload,
    NotificationMarkReadPayload,
    NotificationType,
)
from app.graphql.viewer import authorized_scope


def _errors(exc: ValidationError) -> list[ValidationErrorType]:
    return [ValidationErrorType.from_domain(issue) for issue in exc.issues]


@strawberry.type
class NotificationMutation:
    """The write half of the inbox, merged into the root Mutation.

    Reading is the only thing that happens to a notification here. There is
    no create -- notifications are written by the services that cause them,
    inside the transaction of the change itself, and a mutation that let a
    client file one would be an API for putting things in other people's
    inboxes. There is no delete either: marking read is what a client wants,
    and an inbox that can be emptied by the person who was notified is a
    record that can be destroyed by the person it was about.

    Both resolvers establish identity before touching data, and both scope
    the write to the viewer's own rows. Marking somebody else's notification
    read answers exactly as an id that does not exist.
    """

    @strawberry.mutation
    async def notification_mark_read(
        self,
        info: Info,
        input: NotificationMarkReadInput,
    ) -> NotificationMarkReadPayload:
        """Mark one of the viewer's own notifications read.

        Idempotent: a second call succeeds and returns the same `readAt` the
        first one set, so a retry -- or two tabs -- cannot move the instant.

        A notification belonging to another user, or to another workspace,
        answers "Notification does not exist", which is what an unknown id
        answers. That equivalence is deliberate: "that is not yours" confirms
        the id names a real item somebody really received.
        """
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            entity = await info.context.activity_service.mark_read(
                scope=scope,
                notification_id=input.id,
            )
        except ValidationError as exc:
            # Only expected input validation reaches the payload. Everything
            # else propagates through GraphQL's error mechanism and is masked.
            return NotificationMarkReadPayload(
                notification=None,
                errors=_errors(exc),
            )

        return NotificationMarkReadPayload(
            notification=NotificationType.from_entity(entity),
            errors=[],
        )

    @strawberry.mutation
    async def notification_mark_all_read(
        self,
        info: Info,
        input: NotificationMarkAllReadInput,
    ) -> NotificationMarkAllReadPayload:
        """Clear the viewer's unread notifications in one workspace.

        Returns how many moved, which is what a client needs to zero a badge.
        Running it twice answers 0 the second time rather than the same
        number again -- the statement matches only unread rows -- so the
        count is honest about what this call did rather than about what the
        inbox contains.
        """
        scope = await authorized_scope(info, input.workspace_slug)

        marked = await info.context.activity_service.mark_all_read(scope=scope)

        return NotificationMarkAllReadPayload(marked_count=marked, errors=[])
