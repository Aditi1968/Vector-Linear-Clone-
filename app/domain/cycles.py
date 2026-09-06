from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class CycleEntity:
    """One team's time-boxed iteration.

    Carries no `workspace_id` and no `team_id`, exactly as IssueEntity carries
    no workspace. Both are already known to every caller that can legitimately
    hold one -- a cycle is only ever read through a WorkspaceScope, and only
    ever listed for a team the caller named -- so putting them on the entity
    would add a second, weaker copy of the tenant a copy of which is already
    travelling with the operation. The one on the entity is the one that ends
    up trusted by mistake.

    Pure application code: no Strawberry, FastAPI, asyncpg or PostgreSQL.
    """

    id: UUID
    number: int
    name: str | None
    starts_at: datetime
    ends_at: datetime
    created_at: datetime
    updated_at: datetime
