from datetime import datetime
from uuid import UUID

import strawberry


@strawberry.input
class CycleCreateInput:
    """The cycle to open.

    `team_id` is required and is the client's, not the request's. Cycles are
    per team, so the tenant seam that picks a default team for a new issue
    (app/graphql/tenancy.py) has nothing to say here: a caller that did not
    name a team has not said which team's numbering its `number` belongs to.
    """

    team_id: UUID
    number: int
    starts_at: datetime
    ends_at: datetime
    name: str | None = None


@strawberry.input
class CycleUpdateInput:
    """The cycle's editable state, whole.

    Every field is required except the optional name, because this replaces
    rather than patches -- see `CycleService.update` for why a partial
    update needs a third value beyond "a name" and "null", and what the
    read-modify-write that avoids it would cost.

    `teamId` is absent on purpose: a cycle's team is fixed for its lifetime.
    """

    id: UUID
    number: int
    starts_at: datetime
    ends_at: datetime
    name: str | None = None


@strawberry.input
class IssueSetCycleInput:
    """Which issue, and which cycle to put it in.

    One mutation for both directions. `cycleId` is nullable and defaults to
    null, so omitting it -- or sending null -- takes the issue out of
    whatever cycle it is in. A separate `issueUnsetCycle` would be a second
    mutation with the same body, and a client picking "No cycle" from a
    dropdown would have to branch on which one to call.
    """

    issue_id: UUID
    cycle_id: UUID | None = None
