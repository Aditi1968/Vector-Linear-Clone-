from datetime import datetime
from uuid import UUID

import strawberry


@strawberry.input
class CycleCreateInput:
    """The cycle to open.

    `team_id` is required. Cycles are numbered per team, so a caller that did
    not name a team has not said which team's numbering its `number` belongs
    to. A team from another workspace is refused by `cycles_team_fk` against
    the authorized workspace, inside the insert.
    """

    # Every workspace-scoped mutation names its tenant, and every mutation
    # that takes an `input` names it HERE rather than beside the input. One
    # place per operation, so a client never has to remember which mutations
    # spell it as an argument; the two that take no input at all
    # (`issueArchive`, `cycleDelete`) carry it as a field argument, because
    # inventing a one-field input object for them would be worse.
    #
    # A slug and not a workspace id, deliberately. CLAUDE.md forbids trusting
    # a workspace id from the frontend: the slug is a public string that
    # selects WHAT is being asked about, and `app.graphql.scope` decides
    # whether the session behind the request may act there.
    workspace_slug: str

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

    workspace_slug: str
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

    workspace_slug: str
    issue_id: UUID
    cycle_id: UUID | None = None
