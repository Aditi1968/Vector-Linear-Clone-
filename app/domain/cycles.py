from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class CycleEntity:
    """One team's time-boxed iteration.

    Carries no `workspace_id`, exactly as IssueEntity carries no workspace: a
    cycle is only ever read through a WorkspaceScope, so a tenant on the
    entity would be a second, weaker copy of one that is already travelling
    with the operation -- and the copy on the entity is the one that ends up
    trusted by mistake.

    `team_id` IS carried, and used not to be. The argument for leaving it off
    was that every caller already knows it, and that held while
    `cycles(teamId:)` was the only way to reach a cycle. It stopped holding
    the moment one arrived through `Issue.cycle`, or through a route naming
    only the cycle: the team is then a fact about the row that nobody at the
    call site has, and a screen wanting to scope its issue list to the cycle's
    team -- which is what makes a `cycleId` filter servable by
    issues_workspace_team_cycle_idx -- had no way to learn it. Unlike the
    workspace, the team is not an authorization boundary, so publishing it
    grants nothing: the row was already read under a scope that allowed it.

    Pure application code: no Strawberry, FastAPI, asyncpg or PostgreSQL.
    """

    id: UUID
    team_id: UUID
    number: int
    name: str | None
    starts_at: datetime
    ends_at: datetime
    created_at: datetime
    updated_at: datetime
