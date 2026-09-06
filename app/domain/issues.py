from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Final
from uuid import UUID


class Unset(Enum):
    """The absence of a field from a patch, as a value.

    A partial update has three states per field and Python's `None` only
    expresses two of them: "set this to 42", "set this to nothing", and
    "leave this alone". Collapsing the last two -- which is what a plain
    `assignee_id: UUID | None = None` parameter does -- makes it impossible
    to unassign an issue, because the request that means "clear the
    assignee" is byte-identical to the request that means "I am only
    changing the title".

    An Enum with exactly one member rather than `object()` or a module-level
    class, because that is the sentinel shape type checkers understand: a
    single-member enum narrows under `is` / `is not`, so after
    `if value is not UNSET:` mypy knows the remaining type is `UUID | None`
    and will report the mistakes this type exists to catch. A bare
    `object()` sentinel narrows to nothing and would make every field access
    downstream an unchecked one.

    Pure application code -- no Strawberry, FastAPI, asyncpg or PostgreSQL.
    The GraphQL layer has its own sentinel (`strawberry.UNSET`) and is
    translated onto this one at the boundary; see
    app/graphql/inputs/issue.py.
    """

    UNSET = "unset"


UNSET: Final = Unset.UNSET


# Categories in which an issue is no longer being worked on. The strings are
# `workflow_states.type` values, and migration 005's
# `workflow_states_type_check` is the same vocabulary.
#
# Kept here rather than in a repository because it is the rule `completed_at`
# follows, and that rule is a product decision, not a detail of any one SQL
# statement. 'canceled' is spelled with one L everywhere -- schema, domain and
# API -- because two spellings of it in one system is a constraint violation
# on a value that looks correct to whoever wrote it.
TERMINAL_STATE_CATEGORIES: Final = ("completed", "canceled")


@dataclass(frozen=True, slots=True)
class IssueEntity:
    id: UUID
    team_id: UUID

    # `team_key` and `number` are carried together because neither is useful
    # alone: the key is unique only within a workspace, and the number only
    # within a team. Together they are `identifier` below, which is the name
    # the issue is known by outside this system.
    team_key: str
    number: int

    title: str
    description: str | None
    priority: int

    workflow_state_id: UUID
    assignee_id: UUID | None
    creator_id: UUID | None

    estimate: int | None
    due_date: date | None

    # The cycle this issue is in, or None for none -- which is the ordinary
    # state of an issue and not a missing value. Only the id: an entity that
    # embedded the cycle would have to be loaded with one, and every issue
    # read would then pay for a join whether or not the caller wanted it.
    cycle_id: UUID | None

    # Derived from the workflow state on every write; never set directly.
    # See IssueService.complete_rule for the rule and why it lives there.
    completed_at: datetime | None

    archived_at: datetime | None

    created_at: datetime
    updated_at: datetime

    @property
    def identifier(self) -> str:
        """`ENG-42`: the team's key, a hyphen, and the issue's number.

        A property rather than a stored column. The two parts are already
        stored and are individually constrained -- 005 forbids a hyphen in
        `teams.key` precisely so this rendering has exactly one parse -- so
        a third column holding the concatenation would be a copy that can
        disagree with its own inputs the first time a team is rekeyed.
        """
        return f"{self.team_key}-{self.number}"


@dataclass(frozen=True, slots=True)
class IssuePatch:
    """The fields an update changes, with everything else left alone.

    Every field defaults to UNSET rather than to None, so a patch that names
    nothing changes nothing. Read the docstring on `Unset` for why the
    distinction cannot be dropped.

    `completed_at` is deliberately absent and there is no way to add it
    through this type. It is derived from the resulting workflow state, so a
    caller that could also set it directly would be able to put the two into
    a disagreement that no later write repairs.

    `archived_at` is absent for a different reason: archiving is its own
    operation with its own authorisation story, not a field edit. See
    IssueService.archive.

    The first three admit None even though the columns behind them are NOT
    NULL, and that is a transport constraint surfacing rather than a change
    of meaning. GraphQL makes an input field required exactly when it is
    non-null with no default, so a field that may be omitted from a patch is
    a field that may also arrive as null. The value is representable here so
    that `IssueService._validate_patch` can refuse it as a field error;
    nothing downstream should ever see one.

    Pure application code -- no Strawberry, FastAPI, asyncpg or PostgreSQL.
    """

    title: str | None | Unset = UNSET
    description: str | None | Unset = UNSET
    priority: int | None | Unset = UNSET
    workflow_state_id: UUID | None | Unset = UNSET
    assignee_id: UUID | None | Unset = UNSET
    estimate: int | None | Unset = UNSET
    due_date: date | None | Unset = UNSET

    @property
    def is_empty(self) -> bool:
        """Whether this patch would change nothing at all."""
        return all(
            value is UNSET
            for value in (
                self.title,
                self.description,
                self.priority,
                self.workflow_state_id,
                self.assignee_id,
                self.estimate,
                self.due_date,
            )
        )
