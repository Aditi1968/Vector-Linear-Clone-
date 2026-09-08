from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final
from uuid import UUID

from app.domain.estimates import EstimateScale


class WorkflowStateCategory(StrEnum):
    """The fixed vocabulary application code is allowed to branch on.

    A workflow state's *name* belongs to the team that owns it: 'Todo' can
    be renamed to 'Up Next', duplicated, reordered or deleted, and none of
    that may change what any code does. The category is the part that does
    not move, so it is the only part code compares against.

    The values are the strings `workflow_states.type` stores, and
    `workflow_states_type_check` in migration 005 is the same list. The two
    have to agree; a value added here without the matching migration
    produces a CHECK violation on write, which is the loud failure and the
    one to prefer.

    `StrEnum` rather than `Enum` so that a member compares equal to the
    string asyncpg hands back, and so the value read out of a row can be
    turned into a member with `WorkflowStateCategory(row["category"])`
    without a lookup table.

    Pure application code -- no Strawberry, FastAPI, asyncpg or PostgreSQL.
    The GraphQL layer wraps this enum rather than restating it; see
    `app/graphql/types/team.py`.
    """

    BACKLOG = "backlog"
    UNSTARTED = "unstarted"
    STARTED = "started"
    COMPLETED = "completed"
    # One L, everywhere: schema, domain and API. Two spellings of this word
    # in one system is a CHECK violation on a value that looks right to
    # whoever wrote it.
    CANCELED = "canceled"


@dataclass(frozen=True, slots=True)
class WorkflowStateEntity:
    """One status on one team's board."""

    id: UUID
    workspace_id: UUID
    team_id: UUID
    name: str
    category: WorkflowStateCategory
    position: int
    color: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TeamEntity:
    """A team, and the key its issue identifiers are prefixed with.

    `key` is unique within the workspace and nowhere wider, so it is only
    ever meaningful alongside `workspace_id`. Nothing may look a team up by
    key alone.
    """

    id: UUID
    workspace_id: UUID
    key: str
    name: str

    # What this team's estimates count -- points, hours, t-shirt sizes, or
    # nothing named. migration 006 stored `issues.estimate INTEGER` with no
    # unit and said so out loud: "a limit the product wants is a product
    # policy, enforced where the product knows the team's unit". This is where
    # the unit is known.
    #
    # On the TEAM and not the workspace, because estimation is a team practice:
    # one workspace legitimately holds an engineering team on points and a
    # support team on hours, and `issues.team_id` is NOT NULL so the scale an
    # estimate is in is never ambiguous. Defaulted to NONE by
    # migrations/029_estimates_dates.sql, which is what every estimate written
    # before it already meant.
    estimate_scale: EstimateScale

    created_at: datetime


@dataclass(frozen=True, slots=True)
class TeamWorkflow:
    """A team together with the states its issues can occupy.

    The pair exists so that listing teams and their workflows is two
    queries in total rather than one per team. The states are a tuple
    because this is a value read out of the database, not a collection a
    caller is invited to append to.
    """

    team: TeamEntity
    workflow_states: tuple[WorkflowStateEntity, ...]


# The board every new team starts with: one state per category, in the order a
# board renders them.
#
# The same five rows migrations/005_team_workflows.sql seeds onto every team
# that existed when it ran, restated here because a team created afterwards has
# to get the same board and a migration cannot reach forward to do it. The two
# copies are pinned equal by tests/test_members_invites_db.py, which compares a
# team this code creates against the team 005 seeded.
#
# The names are defaults a team may rename freely; nothing may look a state up
# by name. `category` is the part that does not move -- see WorkflowStateCategory.
DEFAULT_WORKFLOW_STATES: Final[
    tuple[tuple[str, WorkflowStateCategory, int, str], ...]
] = (
    ("Backlog", WorkflowStateCategory.BACKLOG, 0, "#bec2c8"),
    ("Todo", WorkflowStateCategory.UNSTARTED, 1, "#e2e2e2"),
    ("In Progress", WorkflowStateCategory.STARTED, 2, "#f2c94c"),
    ("Done", WorkflowStateCategory.COMPLETED, 3, "#5e6ad2"),
    ("Canceled", WorkflowStateCategory.CANCELED, 4, "#95a2b3"),
)
