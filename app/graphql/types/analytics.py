"""The analytics aggregate, as the API publishes it.

Every type here is a projection of one in `app.domain.analytics` and adds no
field the domain does not hold. The descriptions are longer than usual on
purpose: a number on a metrics screen is only as good as the reader's ability
to know what it counts, and this is where a client author reads that.
"""

from datetime import date, datetime
from uuid import UUID

import strawberry

from app.domain.analytics import (
    ANALYTICS_MAX_DAYS,
    AssigneeLoad,
    CategoryCount,
    CycleProgress,
    CycleTimeSummary,
    DurationSummary,
    PriorityCount,
    ProjectProgress,
    TeamCompletion,
    ThroughputDay,
    ThroughputTotals,
    WorkspaceAnalytics,
)
from app.domain.estimates import EstimateScale
from app.domain.teams import WorkflowStateCategory

# Imported for their side effect, exactly as `app/graphql/inputs/issue.py` and
# `app/graphql/types/saved_view.py` import them. `strawberry.enum` annotates
# the DOMAIN enums with their GraphQL definitions and returns the same class
# objects, so the fields below are annotated with the domain names -- but a
# field referencing one of those classes before this module has run would find
# no definition on it, and importing this file is what guarantees it has.
from app.graphql.types.team import (  # noqa: F401
    EstimateScaleType,
    WorkflowStateCategoryType,
)


@strawberry.type(
    name="ThroughputDay",
    description=(
        "One UTC calendar day, and the work that stopped on it. Days are UTC "
        "for every viewer -- nothing in this schema records a person's "
        "timezone -- so a completion late in the evening west of Greenwich "
        "lands on the following day's point."
    ),
)
class ThroughputDayType:
    day: date
    created: int = strawberry.field(
        description=(
            "Issues FILED on this day. From `created_at`, so it counts a "
            "different event from the other two and an issue can appear on "
            "one day here and another day there."
        )
    )
    completed: int = strawberry.field(
        description=(
            "Issues that reached a workflow state in the COMPLETED category "
            "on this day."
        )
    )
    canceled: int = strawberry.field(
        description=(
            "Issues that reached a CANCELED state on this day. Reported "
            "separately and never added to `completed`: `issues.completed_at` "
            "is stamped for both terminal categories, so a single closed "
            "count would report abandonment as delivery."
        )
    )

    @classmethod
    def from_domain(cls, entity: ThroughputDay) -> "ThroughputDayType":
        return cls(
            day=entity.day,
            created=entity.created,
            completed=entity.completed,
            canceled=entity.canceled,
        )


@strawberry.type(
    name="ThroughputTotals",
    description=(
        "The window's three series added up, and the completion rate derived "
        "from two of them. Summed from the same points the chart draws, so a "
        "headline can never disagree with the series under it."
    ),
)
class ThroughputTotalsType:
    created: int
    completed: int
    canceled: int
    completion_rate: float | None = strawberry.field(
        description=(
            "completed / (completed + canceled): of the work that STOPPED in "
            "this window, the share that was delivered rather than abandoned. "
            "Deliberately NOT completed/created -- the issues finished this "
            "month are mostly not the ones filed this month, so that quotient "
            "can exceed 1 and is not a proportion of anything. Null when "
            "nothing stopped; 0 would report a failure that did not happen."
        )
    )

    @classmethod
    def from_domain(cls, entity: ThroughputTotals) -> "ThroughputTotalsType":
        return cls(
            created=entity.created,
            completed=entity.completed,
            canceled=entity.canceled,
            completion_rate=entity.completion_rate,
        )


@strawberry.type(
    name="DurationSummary",
    description=(
        "A distribution of durations in hours: median and 90th percentile, "
        "with the sample size they are over. Percentiles rather than a mean, "
        "because one two-year-old issue moves a mean by more than a week's "
        "real work."
    ),
)
class DurationSummaryType:
    count: int = strawberry.field(
        description=(
            "How many issues the two percentiles are over. A median over "
            "three issues is a number that will move next week; this is what "
            "lets a reader see that."
        )
    )
    median_hours: float
    p90_hours: float = strawberry.field(
        description="The 90th percentile, interpolated between samples."
    )

    @classmethod
    def from_domain(cls, entity: DurationSummary) -> "DurationSummaryType":
        return cls(
            count=entity.count,
            median_hours=entity.median_hours,
            p90_hours=entity.p90_hours,
        )


@strawberry.type(
    name="CycleTimeSummary",
    description=(
        "How long delivered work spent BEING WORKED ON -- first transition "
        "into a started state, to completion -- together with how much of the "
        "work that covers. This schema has no `started_at` column, so the "
        "start instant is read from the activity history, which begins at one "
        "migration and records nothing for an issue dragged straight from "
        "backlog to done. Compare `measured` against `completedTotal` before "
        "quoting the median, and read `leadTime` where the coverage is thin."
    ),
)
class CycleTimeSummaryType:
    measured: int = strawberry.field(
        description=(
            "Completed issues that had a recorded start, and so are in the "
            "percentiles. Zero is a real answer: it means no delivered issue "
            "in this window has a usable start, and the two hour figures "
            "below are then meaningless and must not be shown."
        )
    )
    completed_total: int = strawberry.field(
        description=(
            "Completed issues in this window, measurable or not. The "
            "denominator of the coverage, and the same number as "
            "`leadTime.count`."
        )
    )
    median_hours: float
    p90_hours: float = strawberry.field(
        description="The 90th percentile, interpolated between samples."
    )

    @classmethod
    def from_domain(cls, entity: CycleTimeSummary) -> "CycleTimeSummaryType":
        return cls(
            measured=entity.measured,
            completed_total=entity.completed_total,
            median_hours=entity.median_hours,
            p90_hours=entity.p90_hours,
        )


@strawberry.type(
    name="PriorityCount",
    description=(
        "Live, unfinished issues at one priority level. `priority` is the "
        "stored 0-4, where 0 means NO priority rather than the lowest one. "
        "Naming the levels is the client's job."
    ),
)
class PriorityCountType:
    priority: int
    issues: int

    @classmethod
    def from_domain(cls, entity: PriorityCount) -> "PriorityCountType":
        return cls(priority=entity.priority, issues=entity.issues)


@strawberry.type(
    name="ProjectProgress",
    description=(
        "One project's live issues and how many are done. Two counts rather "
        "than a percentage: 2 of 5 and 200 of 500 are the same fraction and "
        "are not equally worth acting on. Both exclude archived issues, so "
        "filing work away cannot make progress fall."
    ),
)
class ProjectProgressType:
    project_id: UUID
    name: str
    state: str = strawberry.field(
        description=(
            "The project's own state: planned, started, paused, completed or "
            "canceled. Independent of its issues -- a project can be marked "
            "completed with issues still open, and the screen shows both."
        )
    )
    issues: int
    completed: int

    @classmethod
    def from_domain(cls, entity: ProjectProgress) -> "ProjectProgressType":
        return cls(
            project_id=entity.project_id,
            name=entity.name,
            state=entity.state,
            issues=entity.issues,
            completed=entity.completed,
        )


@strawberry.type(
    name="CycleProgress",
    description=(
        "One cycle's scope and delivery, in its own team's estimate unit. "
        "Counts are over the cycle's WHOLE scope, not the part of it inside "
        "the requested window -- a sprint's progress is the sprint's. Cycles "
        "are listed when their span overlaps the window."
    ),
)
class CycleProgressType:
    cycle_id: UUID
    number: int
    name: str | None
    starts_at: datetime
    ends_at: datetime
    team_key: str = strawberry.field(
        description=(
            "The owning team's key. A cycle belongs to exactly one team, "
            "which is why a single estimate scale applies to the whole row."
        )
    )
    estimate_scale: EstimateScale
    issues: int = strawberry.field(description="Live issues currently in this cycle.")
    completed: int
    estimated: int = strawberry.field(
        description="How many of those completions carried an estimate at all."
    )
    completed_estimate: int | None = strawberry.field(
        description=(
            "The sum of the completed issues' estimates, in this team's unit. "
            "Null for a t-shirt team -- the stored integer is a rung on a "
            "ladder, so two of them do not add to a size -- and null when "
            "nothing was estimated. Read `estimated` to tell the two apart. "
            "Never add this across cycles: two cycles may be on two scales."
        )
    )

    @classmethod
    def from_domain(cls, entity: CycleProgress) -> "CycleProgressType":
        return cls(
            cycle_id=entity.cycle_id,
            number=entity.number,
            name=entity.name,
            starts_at=entity.starts_at,
            ends_at=entity.ends_at,
            team_key=entity.team_key,
            estimate_scale=entity.estimate_scale,
            issues=entity.issues,
            completed=entity.completed,
            estimated=entity.estimated,
            completed_estimate=entity.completed_estimate,
        )


@strawberry.type(
    name="StateCategoryCount",
    description=(
        "Live issues sitting in one workflow-state category, as of now. A "
        "snapshot with no time dimension: the historical version needs a "
        "complete state history, which begins only at the activity migration."
    ),
)
class StateCategoryCountType:
    category: WorkflowStateCategory
    issues: int = strawberry.field(
        description=(
            "Unarchived issues in this category. COMPLETED and CANCELED are "
            "two of the five buckets, not an excluded remainder -- finished "
            "work stays on the board until it is archived."
        )
    )

    @classmethod
    def from_domain(cls, entity: CategoryCount) -> "StateCategoryCountType":
        return cls(category=entity.category, issues=entity.issues)


@strawberry.type(
    name="AssigneeWorkload",
    description=(
        "One person's share of the unfinished work. A null `assigneeId` is the "
        "unassigned pile, which is a real bucket and usually the largest."
    ),
)
class AssigneeWorkloadType:
    assignee_id: UUID | None
    name: str | None = strawberry.field(
        description=(
            "The assignee's display name, falling back to their email address "
            "when they have not set one. Null with a null `assigneeId`."
        )
    )
    open_issues: int = strawberry.field(
        description=(
            "Live issues in a BACKLOG, UNSTARTED or STARTED state. Finished "
            "work is excluded: this is what somebody is carrying, not what "
            "they have ever shipped."
        )
    )

    @classmethod
    def from_domain(cls, entity: AssigneeLoad) -> "AssigneeWorkloadType":
        return cls(
            assignee_id=entity.assignee_id,
            name=entity.name,
            open_issues=entity.open_issues,
        )


@strawberry.type(
    name="TeamCompletion",
    description=(
        "What one team delivered in this window, in that team's own estimate "
        "unit. There is deliberately no workspace-wide estimate total: a "
        "workspace whose teams estimate in points, hours and t-shirt sizes has "
        "no unit to sum them into, and t-shirt sizes are a ladder rather than "
        "a quantity at all."
    ),
)
class TeamCompletionType:
    team_id: UUID
    key: str
    name: str
    estimate_scale: EstimateScale = strawberry.field(
        description=(
            "The unit this team's estimates are in, and the reason there is "
            "no workspace-wide total to compare it against."
        )
    )
    completed: int
    estimated: int = strawberry.field(
        description="How many of those completions carried an estimate at all."
    )
    estimate_total: int | None = strawberry.field(
        description=(
            "The sum of those estimates, in this team's unit. Null when the "
            "team estimates in t-shirt sizes -- the stored integer is a "
            "position on a five-rung ladder, so summing two of them produces "
            "no size -- and also null when nothing was estimated. Read "
            "`estimated` to tell the two apart."
        )
    )

    @classmethod
    def from_domain(cls, entity: TeamCompletion) -> "TeamCompletionType":
        return cls(
            team_id=entity.team_id,
            key=entity.key,
            name=entity.name,
            estimate_scale=entity.estimate_scale,
            completed=entity.completed,
            estimated=entity.estimated,
            estimate_total=entity.estimate_total,
        )


@strawberry.type(
    name="WorkspaceAnalytics",
    description=(
        "How one workspace has been moving over a bounded recent window. "
        "Every figure is computed from stored rows at request time; nothing "
        "here is precomputed, and nothing here is estimated. Metrics this "
        "schema cannot compute honestly -- in-progress time, cycle burndown, "
        "and open counts over time -- are absent rather than approximated."
    ),
)
class WorkspaceAnalyticsType:
    range_start: date
    range_end: date = strawberry.field(
        description="The last day of the window, inclusive. Today, in UTC."
    )
    days: int = strawberry.field(
        description=(
            "The window length actually used, which is the one that was asked "
            f"for: a value above {ANALYTICS_MAX_DAYS} is refused rather than "
            "quietly reduced."
        )
    )

    throughput: list[ThroughputDayType] = strawberry.field(
        description=(
            "One point per day in the window, including days on which nothing "
            "happened. A day with a zero is a fact; a day missing from a "
            "series is a hole a renderer will draw a line through."
        )
    )
    totals: ThroughputTotalsType = strawberry.field(
        description="`throughput` summed, and the completion rate it supports."
    )

    lead_time: DurationSummaryType | None = strawberry.field(
        description=(
            "Creation to completion, over work delivered in this window. "
            "Complete -- both instants are columns on the issue -- so this is "
            "the duration to quote when `cycleTime.measured` is small. Null "
            "when nothing was delivered."
        )
    )
    cycle_time: CycleTimeSummaryType | None = strawberry.field(
        description=(
            "Start of work to completion, with its coverage. Partial by "
            "construction; read `measured` against `completedTotal`."
        )
    )
    issue_age: DurationSummaryType | None = strawberry.field(
        description=(
            "How long the UNFINISHED work has been open, as of now. A "
            "snapshot of the backlog, not of the window. Null when nothing is "
            "open."
        )
    )

    state_mix: list[StateCategoryCountType] = strawberry.field(
        description=(
            "A snapshot of now, not of the window. Every live issue, so "
            "COMPLETED and CANCELED are two of the buckets. Categories "
            "holding no issues are omitted."
        )
    )
    priority_mix: list[PriorityCountType] = strawberry.field(
        description=(
            "Live UNFINISHED issues by priority -- a different population "
            "from `stateMix`, and the same one as `workload`, because the "
            "question is what is on the plate now. Levels holding no issues "
            "are omitted. At most five rows: the column is constrained 0-4."
        )
    )

    workload: list[AssigneeWorkloadType] = strawberry.field(
        description=(
            "The busiest assignees, most work first, bounded server-side. "
            "Compare its length against `assigneeTotal` before describing it "
            "as the whole workspace."
        )
    )
    assignee_total: int = strawberry.field(
        description=(
            "How many distinct assignees hold unfinished work, including the "
            "unassigned pile as one. `workload` is a prefix of these."
        )
    )

    teams: list[TeamCompletionType] = strawberry.field(
        description="Teams that delivered something in the window, busiest first."
    )
    team_total: int = strawberry.field(
        description="How many teams delivered something; `teams` may be a prefix."
    )

    projects: list[ProjectProgressType] = strawberry.field(
        description=(
            "Projects holding live issues, largest first, bounded "
            "server-side. Issues in no project are excluded rather than "
            "bucketed: most issues are in none, so that bar would be the "
            "biggest on every chart and would say nothing."
        )
    )
    project_total: int = strawberry.field(
        description="How many projects hold live issues; `projects` may be a prefix."
    )

    cycles: list[CycleProgressType] = strawberry.field(
        description=(
            "Cycles whose span overlaps the window, newest first, bounded "
            "server-side. Each row's counts are over the whole cycle."
        )
    )
    cycle_total: int = strawberry.field(
        description="How many cycles overlap the window; `cycles` may be a prefix."
    )

    overdue: int = strawberry.field(
        description=(
            "Live, unfinished issues whose due date is before today. A thing "
            "due today is not yet late."
        )
    )

    @classmethod
    def from_domain(cls, entity: WorkspaceAnalytics) -> "WorkspaceAnalyticsType":
        return cls(
            range_start=entity.range_start,
            range_end=entity.range_end,
            days=entity.days,
            throughput=[
                ThroughputDayType.from_domain(day) for day in entity.throughput
            ],
            totals=ThroughputTotalsType.from_domain(entity.totals),
            lead_time=(
                None
                if entity.lead_time is None
                else DurationSummaryType.from_domain(entity.lead_time)
            ),
            cycle_time=(
                None
                if entity.cycle_time is None
                else CycleTimeSummaryType.from_domain(entity.cycle_time)
            ),
            issue_age=(
                None
                if entity.issue_age is None
                else DurationSummaryType.from_domain(entity.issue_age)
            ),
            state_mix=[
                StateCategoryCountType.from_domain(count) for count in entity.state_mix
            ],
            priority_mix=[
                PriorityCountType.from_domain(count) for count in entity.priority_mix
            ],
            workload=[
                AssigneeWorkloadType.from_domain(load) for load in entity.workload
            ],
            assignee_total=entity.assignee_total,
            teams=[TeamCompletionType.from_domain(team) for team in entity.teams],
            team_total=entity.team_total,
            projects=[
                ProjectProgressType.from_domain(project) for project in entity.projects
            ],
            project_total=entity.project_total,
            cycles=[CycleProgressType.from_domain(cycle) for cycle in entity.cycles],
            cycle_total=entity.cycle_total,
            overdue=entity.overdue,
        )
