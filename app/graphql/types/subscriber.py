"""Transport types for who is watching an issue."""

from datetime import datetime
from uuid import UUID

import strawberry

from app.domain.subscribers import SubscriberEntity
from app.graphql.types.errors import ValidationErrorType


@strawberry.type(name="IssueSubscriber")
class IssueSubscriberType:
    """One person watching one issue.

    `userId` and no user object. Resolving it would hand every member of a
    workspace a way to turn an issue id into a list of names and email
    addresses, and there is no batching loader for members yet -- so the field
    that reads well would also be one query per watcher. The id is what a
    client needs to say "you are watching this" and to render an avatar from a
    list it already has.

    No `issueId`: every read of these is already scoped to one issue, and a
    field that always says the same thing invites a client to believe it could
    say something else.
    """

    user_id: UUID

    created_at: datetime = strawberry.field(
        description="When this person started watching, which does not move if "
        "they are auto-subscribed again."
    )

    @classmethod
    def from_entity(cls, entity: SubscriberEntity) -> "IssueSubscriberType":
        return cls(user_id=entity.user_id, created_at=entity.created_at)


@strawberry.type
class IssueSubscriptionPayload:
    """Whether the viewer is watching the issue, after the call.

    One payload for both mutations, and it reports the resulting STATE rather
    than whether this call changed it. That is the answer a client needs -- it
    is rendering a toggle -- and it is the one that makes a retry after a
    dropped response indistinguishable from the first attempt, which is what
    "idempotent" has to mean at the transport as well as in the service.

    False beside a non-empty `errors` is the refusal case: an issue that is not
    in this workspace, or not there at all.
    """

    subscribed: bool
    errors: list[ValidationErrorType]
