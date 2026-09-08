from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum, StrEnum
from typing import Final
from uuid import UUID

from app.domain.teams import WorkflowStateCategory


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

    # Where this issue sits in the workspace's project plan, if anywhere.
    # Both are None for the great majority of issues, and that is a real
    # state rather than missing data.
    #
    # `milestone_id` is never set while `project_id` is None:
    # `issues_milestone_requires_project` refuses that row, because a
    # milestone only means anything inside the project that owns it. A
    # reader can therefore take a non-None milestone as implying a project
    # without checking for it.
    project_id: UUID | None
    milestone_id: UUID | None

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


class DueWindow(StrEnum):
    """A relative window of due dates, resolved against the server's today.

    Four windows and not a pair of dates, and the difference is where "today"
    is decided. A client sending `dueBefore: 2026-09-08` has had to work out
    what today is, in whatever zone its browser is in -- and two colleagues in
    different zones then ask for different lists from the same screen. These
    resolve against `CURRENT_DATE` in the statement, so "overdue" is one set for
    everybody in the workspace.

    Which today that is, is UTC, and migrations/029_estimates_dates.sql argues
    it: a due date is a calendar day with no zone (006 refuses TIMESTAMPTZ
    precisely so "due Friday" is the same promise in Berlin and Los Angeles),
    and comparing it against a viewer's local today would make the same issue
    overdue for one of them and not the other. The reminder sweep answers the
    same way, so nothing in the product contradicts anything else in it.

    THIS_WEEK is today and the six days after it, NOT the calendar week. A
    Monday-to-Sunday window is nearly empty by Friday afternoon, which is not
    the list somebody planning their week asked for; a rolling seven days
    answers the same question on every day of the week.

    NONE is the issues with no due date, which is most of them -- an ordinary
    state and not missing data, exactly as 006 says. It is a filter people
    genuinely want: "what have we committed to nothing about".

    A StrEnum for the reason every other vocabulary here is one: the member is
    the spelling, so a value read out of a stored saved-view filter becomes a
    member without a lookup table.
    """

    OVERDUE = "overdue"
    TODAY = "today"
    THIS_WEEK = "this_week"
    NONE = "none"


# How many days THIS_WEEK reaches forward, today included.
#
# Named rather than spelled as a 7 inside a SQL fragment, because a bare
# `+ 7` in a date predicate reads as a count of something.
THIS_WEEK_DAYS: Final = 7


@dataclass(frozen=True, slots=True)
class IssueFilter:
    """What narrows a list of issues, with everything unnamed left wide.

    UNSET rather than None on every field, and for the same reason
    `IssuePatch` needs the sentinel: three states, not two. "Assigned to
    Ana", "assigned to nobody", and "I am not filtering on assignee" are
    three different questions, and a plain `UUID | None` can only ask two of
    them. `assignee_id=None` here means `assignee_id IS NULL` -- the
    unassigned issues -- and UNSET means the filter is absent.

    Only the three columns that are genuinely nullable admit None. A team, a
    workflow state and a priority are NOT NULL on every row, so "has none"
    is not a set anyone can ask for, and the types say so.

    Nothing here is a tenant. Every predicate this becomes is ANDed onto the
    workspace the caller was authorized for, so a `project_id` from another
    workspace narrows to nothing rather than widening to that workspace --
    see IssueRepository.

    Pure application code -- no Strawberry, FastAPI, asyncpg or PostgreSQL.
    """

    team_id: UUID | Unset = UNSET
    assignee_id: UUID | None | Unset = UNSET
    workflow_state_id: UUID | Unset = UNSET
    state_category: WorkflowStateCategory | Unset = UNSET
    label_id: UUID | Unset = UNSET
    priority: int | Unset = UNSET
    project_id: UUID | None | Unset = UNSET
    cycle_id: UUID | None | Unset = UNSET

    # The three due-date filters, and they are three rather than one because
    # they answer two different kinds of question. `due_window` is relative and
    # is resolved by the SERVER against its own today, which is the whole
    # reason it exists -- see `DueWindow`. The two dates are absolute and are
    # exactly what a client already knows how to send: "the sprint", "next
    # month", a range somebody dragged on a calendar.
    #
    # All three narrow and all three may be combined, because they are ANDed
    # like every other predicate here. `due_window: OVERDUE` with
    # `due_after: <a date>` is a coherent request and needs no special case;
    # `due_after` later than `due_before` selects nothing, which is the same
    # empty page an id from another workspace gets rather than an error, for
    # the same reason.
    #
    # None is not admitted on any of them: "no due date" is `DueWindow.NONE`
    # and not a null date, so there is one spelling of that set instead of two
    # that could be sent together and contradict each other.
    due_window: DueWindow | Unset = UNSET
    due_after: date | Unset = UNSET
    due_before: date | Unset = UNSET


class IssueOrderField(StrEnum):
    """What an issue list is sorted by.

    The values are the sort KEY each field names, not necessarily a column:
    `PRIORITY` orders by urgency, and urgency is not what the `priority`
    column sorts as. 0 there means "no priority" rather than "the lowest
    one" -- 1 is Urgent and 4 is Low -- so a plain column sort puts the
    issues nobody has triaged either at the top or below Low, and neither is
    an order a person asked for. The key is `NULLIF(priority, 0)`, which
    sorts 1..4 ascending and leaves the untriaged at the end. See
    `app/repositories/issues.py` for the expression itself and
    `order_key` below for the value a cursor carries.

    Every ordering here is completed by `id` as a tie-break, which is what
    keeps it total: `created_at` ties constantly under a bulk import,
    `priority` has five distinct values across the whole table, and a
    keyset walk over a non-total order silently skips and repeats rows.
    """

    PRIORITY = "priority"
    CREATED_AT = "created_at"
    UPDATED_AT = "updated_at"
    DUE_DATE = "due_date"


class OrderDirection(StrEnum):
    ASC = "asc"
    DESC = "desc"


@dataclass(frozen=True, slots=True)
class IssueOrder:
    """One total ordering of an issue list.

    The default is the ordering this product has always listed issues in,
    so a caller that names no order gets the page it used to get.
    """

    field: IssueOrderField = IssueOrderField.CREATED_AT
    direction: OrderDirection = OrderDirection.DESC

    @property
    def token(self) -> str:
        """This ordering as one opaque string, for a cursor to carry.

        A cursor is only meaningful against the ordering that minted it, so
        the ordering travels inside it and the service refuses a cursor
        replayed under a different one.
        """
        return f"{self.field.value}:{self.direction.value}"


# The value an issue sorts at, for each ordering.
#
# This has to agree with the SQL expression `app/repositories/issues.py`
# orders by, because one mints the cursor and the other resumes from it: a
# disagreement is not an error anywhere, it is a page walk that quietly skips
# rows. `tests/test_issue_ordering.py` walks every field to hold the two
# together.
type OrderKey = int | datetime | date | None


def order_key(issue: IssueEntity, field: IssueOrderField) -> OrderKey:
    if field is IssueOrderField.PRIORITY:
        # NULLIF(priority, 0): untriaged issues have no urgency, not the
        # lowest one. None here is the same "no key" a missing due date is,
        # and the keyset handles both the same way.
        return issue.priority or None

    if field is IssueOrderField.CREATED_AT:
        return issue.created_at

    if field is IssueOrderField.UPDATED_AT:
        return issue.updated_at

    return issue.due_date


# The wide filter and the default ordering, as shared instances.
#
# Both types are frozen, so one instance per default is safe -- and a default
# argument has to be a singleton anyway: `def list(..., issue_filter =
# IssueFilter())` builds a new one at import time and hands the SAME object to
# every caller regardless, which is the bug ruff's B008 is about. Naming them
# also gives the "no filter" case something to be called at a call site.
NO_FILTER: Final = IssueFilter()
DEFAULT_ORDER: Final = IssueOrder()
