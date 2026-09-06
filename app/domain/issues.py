from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class IssueEntity:
    id: UUID
    title: str
    description: str | None
    priority: int

    # The cycle this issue is in, or None for none -- which is the ordinary
    # state of an issue and not a missing value. Only the id: an entity that
    # embedded the cycle would have to be loaded with one, and every issue
    # read would then pay for a join whether or not the caller wanted it.
    cycle_id: UUID | None

    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime
