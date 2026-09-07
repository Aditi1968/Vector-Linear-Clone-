"""Who is watching an issue.

Pure application code -- no Strawberry, FastAPI, asyncpg or PostgreSQL.

Distinct from `app.domain.notifications`, and the two are not layers of one
thing: a subscription is a standing request to hear about an issue, and a
notification is one item already delivered because of it. A subscriber has no
read state and is not addressed to a moment; a notification is both. See
migrations/020_subscribers_templates.sql for why they are separate tables.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class SubscriberEntity:
    """One person watching one issue.

    No `issue_id`, because every read of these is already scoped to one issue
    and a field that always says the same thing invites a client to believe it
    could say something else -- the argument `NotificationType` makes for
    having no `userId`.

    `created_at` is "watching since". It does not move when a subscription is
    re-asserted, which is what makes a re-subscribe a genuine no-op rather than
    a rewrite.
    """

    user_id: UUID
    created_at: datetime
