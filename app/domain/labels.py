from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class LabelEntity:
    """One label, as the product understands it.

    No `workspace_id`. A label is only ever read or written through a
    WorkspaceScope, so carrying the tenant here would be a second copy of a
    fact the caller already holds -- and a second copy is something that can
    disagree. `IssueEntity` omits it for the same reason.
    """

    id: UUID
    name: str
    color: str
    created_at: datetime
    updated_at: datetime
