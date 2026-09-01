from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class IssueEntity:
    id: UUID
    title: str
    description: str | None
    priority: int
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime
