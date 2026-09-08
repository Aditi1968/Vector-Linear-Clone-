"""What the workspace can honestly be told about how it is moving.

Pure application code -- no Strawberry, FastAPI, asyncpg or PostgreSQL.

Every type here is shaped by one rule, and it is the rule that made this
feature a placeholder for as long as it was: a metric this schema cannot
compute from stored rows is not approximated, it is absent. The three that did
not survive that rule are named at the foot of this module, with what would
have to change for each.

The other thing this module holds is the BOUND. An aggregate is the one field
shape in this schema whose cost is set by an argument rather than by a page
size, and `app/graphql/limits.py` prices a document during validation -- where
the value of `days` is not yet known. So the ceiling is enforced here and
restated on the screen, rather than left to a complexity budget that cannot
see it.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Final
from uuid import UUID

from app.domain.estimates import EstimateScale
from app.domain.teams import WorkflowStateCategory


# The window, in days, and the ceiling on it.
#
# 180 is not a round number chosen for looking safe. It is the largest window
# for which the daily series stays a thing a client can render and a person can
# read: one bar per day, so 180 bars, which is already dense on a wide screen
# and is 180 objects on the wire. A year would double both and change nothing
# about what the reader learns.
#
# The floor is 1 rather than 0, because a zero-day window has no buckets and
# `rangeEnd` would precede `rangeStart` -- a range nothing can plot and nobody
# meant to ask for.
#
# A value outside the range is REFUSED and not clamped. Clamping would answer
# a request for 365 days with 180 days of data under a heading that says 365,
# which is the exact failure this whole feature is written to avoid.
ANALYTICS_MIN_DAYS: Final = 1
ANALYTICS_MAX_DAYS: Final = 180
ANALYTICS_DEFAULT_DAYS: Final = 30

# How many assignees the workload breakdown returns, busiest first.
#
# The list is bounded because the number of people in a workspace is not, and
# an unbounded GROUP BY behind a field with no page-size argument is the
# fan-out `app/graphql/limits.py` cannot price. Twenty is what a bar chart can
# label.
#
# The remainder is not silently dropped: `WorkspaceAnalytics.assignee_total`
# carries how many distinct assignees the workspace actually has, so the screen
# can say "20 of 34" instead of implying it is showing everyone.
WORKLOAD_LIMIT: Final = 20

# The same bound, for the per-team table. A workspace with more than fifty
# teams is not a shape this product has met, but "has not happened yet" is not
# a bound, and this is one.
TEAM_LIMIT: Final = 50


@dataclass(frozen=True, slots=True)
class ThroughputDay:
    """One UTC day, and what stopped on it.

    Two counts and not one, because `issues.completed_at` is stamped for both
    terminal categories -- see `TERMINAL_STATE_CATEGORIES` -- so a single
    "closed" number would report cancellations as delivery. They are counted
    separately by the category of the state the issue is sitting in, and the
    screen renders them as two series.

    The day is UTC and is not the reader's. See `analytics_window`.
    """

    day: date
    completed: int
    canceled: int


@dataclass(frozen=True, slots=True)
class CycleTimeSummary:
    """How long the work finished in the window took, end to end.

    Measured created_at -> completed_at, over issues whose state is in the
    `completed` category. Cancellations are excluded: an issue that was
    abandoned after six months did not take six months to deliver.

    Median and p90 rather than a mean, because the distribution is the point.
    One issue filed two years ago and closed last Tuesday moves a mean by more
    than the twenty issues a team actually shipped that week, and a mean is
    what makes a metrics page stop being consulted.

    `count` is the sample the two percentiles are over, and it is published for
    the same reason the percentiles are: a median over three issues is a number
    that will move violently next week, and the reader is entitled to know
    that before drawing a conclusion from it.

    Absent -- None on the field above, not a summary of zeros -- when nothing
    was completed in the window. A zeroed summary and a real one look identical
    on a screen, which is the failure mode this whole module is written against.
    """

    count: int
    median_hours: float
    p90_hours: float


@dataclass(frozen=True, slots=True)
class CategoryCount:
    """How many live issues are sitting in one workflow-state category.

    A snapshot of now, with no time dimension at all, and that limitation is
    the honest one: reconstructing "how many were open on 3 March" needs a
    complete state history, and this schema does not have one. See the note at
    the foot of this module.

    "Live" is `archived_at IS NULL` and nothing else, so COMPLETED and CANCELED
    are two of the five buckets rather than an excluded remainder -- an issue
    finished last week is still on the board until somebody files it away, and
    a chart that dropped those would not add up to the board the reader is
    looking at.

    Categories, never state names: 'Done' is a label a team may rename, and a
    workspace where one team calls it 'Shipped' would otherwise produce two
    bars for one thing.
    """

    category: WorkflowStateCategory
    issues: int


@dataclass(frozen=True, slots=True)
class AssigneeLoad:
    """One person's share of the live, unarchived work.

    `assignee_id` is None for the unassigned pile, which is a real bucket and
    usually the largest one -- an issue nobody has picked up is the ordinary
    state of a new issue, and it is exactly what a distribution chart should
    show. `name` is None with it.
    """

    assignee_id: UUID | None
    name: str | None
    open_issues: int


@dataclass(frozen=True, slots=True)
class TeamCompletion:
    """What one team finished in the window, in that team's own unit.

    This type exists because of `teams.estimate_scale`, and it is the whole
    answer to the mixed-scale problem. Adding a workspace-wide "points
    delivered" would require summing an INTEGER column whose meaning is set
    per team: a workspace with one team on hours, one on story points and one
    on t-shirt sizes would produce a total in which 8 means a day's work, half
    a sprint, and nothing at all -- t-shirt sizes are a LADDER, so their sum is
    not a quantity in any unit. There is no conversion between the four, and
    inventing one would be this server deciding a team's process for it.

    So estimates are never summed across teams. They are summed WITHIN a team,
    reported beside the scale that team chose, and the screen prints the unit
    next to every figure. A workspace total is the one number this feature
    refuses to produce.

    `estimate_total` is None when the team estimates in t-shirt sizes, and that
    is not missing data: 2 + 3 on a ladder of names is not 5 of anything.
    `estimated` and `completed` are still reported for those teams, because
    "how much of what we finished was even sized" is a question with the same
    answer in every unit.
    """

    team_id: UUID
    key: str
    name: str
    estimate_scale: EstimateScale
    completed: int
    estimated: int
    estimate_total: int | None


@dataclass(frozen=True, slots=True)
class WorkspaceAnalytics:
    """Everything one analytics page reads, for one workspace, in one window.

    One aggregate rather than six root fields, because they share a window and
    a tenant and are rendered together: six fields would let a document ask for
    six different windows in one request, which is six times the work for a
    screen that shows one.
    """

    range_start: date
    range_end: date
    days: int

    throughput: list[ThroughputDay]
    cycle_time: CycleTimeSummary | None
    state_mix: list[CategoryCount]

    workload: list[AssigneeLoad]
    # How many distinct assignees hold live work, INCLUDING the unassigned
    # bucket if there is one. `workload` is the busiest WORKLOAD_LIMIT of
    # these, so the screen can say what it is not showing.
    assignee_total: int

    teams: list[TeamCompletion]
    # As above: how many teams had work in the window, of which `teams` holds
    # at most TEAM_LIMIT.
    team_total: int

    overdue: int


def analytics_window(days: int, today: date) -> tuple[date, date]:
    """The inclusive first and last day of a `days`-long window ending today.

    Pure, and separate from the service, because "does a 30-day window contain
    30 buckets and end today" is a question worth answering without a database
    or a clock.

    Days are UTC, and that is a real limitation rather than a detail. A
    completion at 23:00 in Los Angeles lands in the following UTC day, so a
    reader on the US west coast sees their evening's work on tomorrow's bar.
    The alternative is a per-viewer timezone, which nothing in this schema
    records -- `users` has no timezone column and no setting writes one.
    Choosing the server's local zone instead would be worse: the same workspace
    would report different numbers from two deployments.

    ponytail: UTC day boundaries, workspace-wide. The upgrade path is a
    timezone on `workspaces` (or on `users`), passed to the bucketing
    expression in `AnalyticsRepository.completion_series`; nothing else moves.
    """
    return (today - timedelta(days=days - 1), today)


def window_bounds(range_start: date, days: int) -> tuple[datetime, datetime]:
    """The window as the half-open UTC instant range a SQL predicate wants.

    `[start of range_start, start of the day after the last day)`. Half-open
    rather than BETWEEN, so that an issue completed at exactly midnight belongs
    to one bucket rather than to two -- and so that the upper bound needs no
    "23:59:59.999999", which is a value that is wrong by a microsecond and
    looks right.
    """
    start = datetime.combine(range_start, time.min, tzinfo=timezone.utc)

    return (start, start + timedelta(days=days))


def dense_throughput(
    counted: dict[date, tuple[int, int]],
    range_start: date,
    days: int,
) -> list[ThroughputDay]:
    """Every day in the window, including the ones nothing happened on.

    The database returns a row per day that has completions; a chart needs a
    point per day in the window. Filling the gaps HERE rather than with a
    `generate_series` LEFT JOIN keeps the statement a plain aggregate and makes
    the filling testable without a container.

    The distinction being preserved is the one this feature is about: a day
    with a zero is a day on which nothing was finished, which is a fact. A day
    that is simply missing from a series is a hole the renderer has to guess
    about, and renderers guess by drawing a straight line through it.
    """
    return [
        ThroughputDay(day=day, completed=completed, canceled=canceled)
        for day, (completed, canceled) in (
            (
                range_start + timedelta(days=offset),
                counted.get(range_start + timedelta(days=offset), (0, 0)),
            )
            for offset in range(days)
        )
    ]


# ---------------------------------------------------------------------------
# What this module does NOT define, and why
# ---------------------------------------------------------------------------
#
# * IN-PROGRESS TIME (first `started` transition -> completion). `issue_activity`
#   records `state_changed` with the workflow-state ids in `from_value` and
#   `to_value`, so the transition is joinable back to `workflow_states.type` and
#   the query is writable. It is not written, for two reasons that compound.
#   Migration 012 arrived after 011, so every issue created before it has no
#   history at all and would be silently absent from the sample. And an issue
#   moved straight from Backlog to Done never entered a `started` state, so it
#   has no start to measure from -- and dropping those issues biases the median
#   towards exactly the work that was tracked most carefully. A median over "the
#   issues that happen to have a recorded start" is not the team's cycle time,
#   and labelling it as such is the lie this feature exists to not tell.
#   To build it: report it beside its own coverage ("over 41 of 96 completed
#   issues"), the way `features/semanticSearch` reports index coverage.
#
# * CYCLE BURNDOWN. A burndown needs the scope of a cycle as it was on each
#   past day: issues added and removed mid-cycle, and their estimates as they
#   were then. `issue_activity` records `cycle_changed` but records no estimate
#   change at all (`app.domain.activity.changes` compares six fields and
#   `estimate` is not one), so the remaining-work line could only be drawn by
#   assuming today's estimates always applied. That assumption makes a chart
#   that is smooth, plausible and wrong.
#   To build it: add `estimate_changed` to `ActivityKind` and to
#   `app.domain.activity.changes`, and accept that the line only starts being
#   true from that migration forward.
#
# * OPEN/CLOSED OVER TIME. The same missing history in its most tempting form.
#   "How many were open on each day" is reconstructible only from a complete
#   record of every state transition, and the record starts at 012.
#   `state_mix` above is the honest subset: a snapshot of now, labelled as one.
