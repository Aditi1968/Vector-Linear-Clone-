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

# And for the two breakdowns that group by a row a workspace can create without
# limit. Projects and cycles are both unbounded in the schema -- nothing caps
# either -- so a workspace with a thousand projects would otherwise return a
# thousand rows behind a field the complexity budget prices as one.
#
# Both carry a `_total` beside them, as `workload` and `teams` do, so the screen
# says "25 of 118" rather than implying the table is the whole plan.
PROJECT_LIMIT: Final = 25
CYCLE_LIMIT: Final = 12


@dataclass(frozen=True, slots=True)
class ThroughputDay:
    """One UTC day: what was filed on it, and what stopped on it.

    Three counts and not one. `created` and the two terminal counts come from
    two different columns -- `created_at` and `completed_at` -- so they are two
    statements merged onto one calendar, not one GROUP BY.

    Completed and canceled are separate because `issues.completed_at` is
    stamped for both terminal categories -- see `TERMINAL_STATE_CATEGORIES` --
    so a single "closed" number would report abandonment as delivery. They are
    told apart by the category of the state the issue is sitting in.

    The day is UTC and is not the reader's. See `analytics_window`.
    """

    day: date
    created: int
    completed: int
    canceled: int


@dataclass(frozen=True, slots=True)
class ThroughputTotals:
    """The window's three counts added up, and the rate derived from them.

    Published rather than left to the client, because `completion_rate` has
    exactly one correct denominator and a client that picked its own would get
    a different number under the same label.
    """

    created: int
    completed: int
    canceled: int

    # completed / (completed + canceled): of the work that STOPPED in this
    # window, the share that was delivered rather than abandoned.
    #
    # Deliberately not completed/created, which is the other obvious reading
    # and is not a rate at all: the issues finished this month are mostly not
    # the issues filed this month, so the quotient can exceed 1 and means
    # nothing at either end. This denominator is the one every numerator row
    # is drawn from, which is what makes it a proportion.
    #
    # None when nothing stopped. 0.0 would render as "0% delivered" for a
    # quiet fortnight in which nothing was abandoned either.
    completion_rate: float | None


@dataclass(frozen=True, slots=True)
class DurationSummary:
    """A distribution of durations, in hours.

    Median and p90 rather than a mean, because the distribution is the point.
    One issue filed two years ago and closed last Tuesday moves a mean by more
    than the twenty issues a team actually shipped that week, and a mean is
    what makes a metrics page stop being consulted.

    `count` is the sample the two percentiles are over, and it is published for
    the same reason the percentiles are: a median over three issues is a number
    that will move violently next week, and the reader is entitled to know
    that before drawing a conclusion from it.

    Absent -- None on the field holding it, not a summary of zeros -- when the
    sample is empty. A zeroed summary and a real one look identical on a
    screen, which is the failure mode this whole module is written against.
    """

    count: int
    median_hours: float
    p90_hours: float


@dataclass(frozen=True, slots=True)
class CycleTimeSummary:
    """How long delivered work spent being WORKED ON, and over how much of it.

    Measured from the first recorded transition into a `started` state to
    `completed_at`. That start instant is not on the issue row -- there is no
    `started_at` column in this schema, in any migration -- so it comes from
    `issue_activity`, which records `state_changed` with the workflow-state ids
    in `from_value` and `to_value`.

    THE COVERAGE, which is why this type is not a plain `DurationSummary`.

    `issue_activity` begins at migration 012, so an issue completed after a
    life spent entirely before that migration has no history to read a start
    from. And an issue dragged straight from Backlog to Done never entered a
    `started` state at all, so it has no start to measure even with a complete
    history. Both are silently ABSENT from the sample.

    Dropping them and publishing the median alone would be the lie: the issues
    that do have a recorded start are disproportionately the ones that were
    worked deliberately, in small steps, by people who move cards -- so the
    median of the measurable is systematically better than the median of the
    work. `measured` and `completed_total` are therefore returned together and
    the screen prints "over 41 of 96 completed issues" beside every figure,
    rather than a number that looks like it is about all of them.

    `lead_time` on `WorkspaceAnalytics` is the one with no coverage gap --
    both its instants are columns on the row -- and is the figure to read when
    this one covers little.
    """

    measured: int
    completed_total: int
    median_hours: float
    p90_hours: float


@dataclass(frozen=True, slots=True)
class PriorityCount:
    """How many live, unfinished issues sit at one priority.

    `priority` is the stored SMALLINT, 0-4, and 0 is "no priority" rather than
    the lowest one -- the same reading `IssueOrder.PRIORITY` encodes as
    `NULLIF(priority, 0)`. Naming the levels is the client's job: this is the
    layer that knows what the column stores, not what a team calls it.

    Unfinished rather than every live issue, matching `AssigneeLoad`: the
    question a priority breakdown answers is what is on the plate now, and
    every urgent issue ever shipped would swamp it otherwise.
    """

    priority: int
    issues: int


@dataclass(frozen=True, slots=True)
class ProjectProgress:
    """One project's live issues, and how many of them are done.

    Two numbers rather than a percentage, because a percentage over three
    issues and a percentage over three hundred render identically and are not
    equally worth acting on. The client divides and shows both.

    Archived issues are excluded from both, so the ratio is over one
    population. Counting completions that had since been filed away against a
    total that had not would let a project's progress fall when somebody tidied
    up.
    """

    project_id: UUID
    name: str
    state: str
    issues: int
    completed: int


@dataclass(frozen=True, slots=True)
class CycleProgress:
    """One cycle's scope and delivery, in its own team's estimate unit.

    Velocity and progress are the same query, because they are the same two
    numbers read for different reasons: `completed` against `issues` is
    progress, and `completed` (or `completed_estimate`) against the cycle's
    length is velocity. Splitting them would be two scans of the same rows.

    A cycle belongs to exactly one team -- `cycles.team_id` is NOT NULL -- so
    unlike `TeamCompletion` there is no mixed-scale problem WITHIN a row. There
    is still one ACROSS rows, which is why `estimate_scale` is carried on every
    one and why nothing here sums two cycles together.

    `completed_estimate` is None for a team on the t-shirt scale, for the
    reason `TeamCompletion` sets out: the stored integer is a rung on a ladder,
    and the sum of two rungs is not a size.

    A CURRENT cycle is not marked as such here. `cycles` has no completed flag
    -- only `starts_at` and `ends_at` -- so whether a cycle is running is a
    comparison against the clock, and the client already has one.
    """

    cycle_id: UUID
    number: int
    name: str | None
    starts_at: datetime
    ends_at: datetime
    team_key: str
    estimate_scale: EstimateScale
    issues: int
    completed: int
    estimated: int
    completed_estimate: int | None


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
    # The three series added up, and the completion rate derived from two of
    # them. Computed from `throughput` rather than from a seventh statement:
    # a sum over 180 integers already on the wire is not worth a scan.
    totals: ThroughputTotals

    # Both durations, always both. `lead_time` is complete and `cycle_time` is
    # partial, and publishing only one of them would either overstate the
    # coverage or throw away the more useful number.
    lead_time: DurationSummary | None
    cycle_time: CycleTimeSummary | None
    # How old the UNFINISHED work is, right now. A snapshot, like `state_mix`:
    # it is about the backlog as it stands, not about the window.
    issue_age: DurationSummary | None

    state_mix: list[CategoryCount]
    priority_mix: list[PriorityCount]

    workload: list[AssigneeLoad]
    # How many distinct assignees hold live work, INCLUDING the unassigned
    # bucket if there is one. `workload` is the busiest WORKLOAD_LIMIT of
    # these, so the screen can say what it is not showing.
    assignee_total: int

    teams: list[TeamCompletion]
    # As above: how many teams had work in the window, of which `teams` holds
    # at most TEAM_LIMIT.
    team_total: int

    projects: list[ProjectProgress]
    project_total: int

    cycles: list[CycleProgress]
    cycle_total: int

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
    stopped: dict[date, tuple[int, int]],
    created: dict[date, int],
    range_start: date,
    days: int,
) -> list[ThroughputDay]:
    """Every day in the window, including the ones nothing happened on.

    Two dicts because they come from two statements over two columns:
    `stopped` is keyed on `completed_at` and `created` on `created_at`, and no
    single GROUP BY can bucket a row by both. Merging them onto one calendar is
    this function's whole job.

    The database returns a row per day that has activity; a chart needs a point
    per day in the window. Filling the gaps HERE rather than with a
    `generate_series` LEFT JOIN keeps both statements plain aggregates and
    makes the filling testable without a container.

    The distinction being preserved is the one this feature is about: a day
    with a zero is a day on which nothing was finished, which is a fact. A day
    that is simply missing from a series is a hole the renderer has to guess
    about, and renderers guess by drawing a straight line through it.
    """
    days_in_window = (range_start + timedelta(days=offset) for offset in range(days))

    return [
        ThroughputDay(
            day=day,
            created=created.get(day, 0),
            completed=stopped.get(day, (0, 0))[0],
            canceled=stopped.get(day, (0, 0))[1],
        )
        for day in days_in_window
    ]


def throughput_totals(series: list[ThroughputDay]) -> ThroughputTotals:
    """The window's three sums, and the one rate they support.

    Derived from the series already computed rather than from a statement of
    its own, so the headline figure and the chart under it can never disagree
    -- which is the way a metrics screen loses a reader's trust fastest.
    """
    created = sum(day.created for day in series)
    completed = sum(day.completed for day in series)
    canceled = sum(day.canceled for day in series)
    stopped = completed + canceled

    return ThroughputTotals(
        created=created,
        completed=completed,
        canceled=canceled,
        # None rather than 0.0 on an empty window: a fortnight in which nothing
        # was finished AND nothing was abandoned has no delivery share, and
        # printing "0%" for it reports a failure that did not happen.
        completion_rate=None if stopped == 0 else completed / stopped,
    )


# ---------------------------------------------------------------------------
# What this module does NOT define, and why
# ---------------------------------------------------------------------------
#
# * A WORKSPACE-WIDE ESTIMATE TOTAL -- "points delivered", the number every
#   velocity chart in this category leads with. `teams.estimate_scale` (029)
#   makes it unanswerable: 8 means a day, half a sprint, or nothing at all
#   depending on which team wrote it, and `tshirt` stores a rung on a ladder
#   rather than a quantity, so its column does not sum to a size in any unit.
#   There is no conversion between the four scales and inventing one would be
#   this server deciding a team's process for it. Estimates are therefore
#   summed WITHIN a team or a cycle and never across them -- see
#   `TeamCompletion` and `CycleProgress`, both of which carry the scale on
#   every row so the screen can print the unit beside every figure.
#   To build it: nothing to build. A workspace whose teams agree on a scale can
#   add the column up itself, and one whose teams do not has no total.
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
