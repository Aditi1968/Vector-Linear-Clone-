from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


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
