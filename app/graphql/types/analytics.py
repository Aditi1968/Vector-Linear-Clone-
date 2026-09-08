"""The analytics aggregate, as the API publishes it.

Every type here is a projection of one in `app.domain.analytics` and adds no
field the domain does not hold. The descriptions are longer than usual on
purpose: a number on a metrics screen is only as good as the reader's ability
to know what it counts, and this is where a client author reads that.
"""

from datetime import date
from uuid import UUID

import strawberry

from app.domain.analytics import (
    ANALYTICS_MAX_DAYS,
    AssigneeLoad,
    CategoryCount,
    CycleTimeSummary,
    TeamCompletion,
    ThroughputDay,
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
            completed=entity.completed,
            canceled=entity.canceled,
        )


@strawberry.type(
    name="CycleTimeSummary",
    description=(
        "How long the work delivered in this window took, from creation to "
        "completion. Cancellations are excluded. Null when nothing was "
        "delivered -- a summary of zeros and a real one look identical."
    ),
)
class CycleTimeSummaryType:
    count: int = strawberry.field(
        description=(
            "How many completions the two percentiles are over. A median over "
            "three issues is a number that will move next week; this is what "
            "lets a reader see that."
        )
    )
    median_hours: float
    p90_hours: float = strawberry.field(
        description="The 90th percentile, interpolated between samples."
    )

    @classmethod
    def from_domain(cls, entity: CycleTimeSummary) -> "CycleTimeSummaryType":
        return cls(
            count=entity.count,
            median_hours=entity.median_hours,
            p90_hours=entity.p90_hours,
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
            "finished. A day with a zero is a fact; a day missing from a "
            "series is a hole a renderer will draw a line through."
        )
    )
    cycle_time: CycleTimeSummaryType | None
    state_mix: list[StateCategoryCountType] = strawberry.field(
        description=(
            "A snapshot of now, not of the window. Categories holding no "
            "issues are omitted."
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
            cycle_time=(
                None
                if entity.cycle_time is None
                else CycleTimeSummaryType.from_domain(entity.cycle_time)
            ),
            state_mix=[
                StateCategoryCountType.from_domain(count) for count in entity.state_mix
            ],
            workload=[
                AssigneeWorkloadType.from_domain(load) for load in entity.workload
            ],
            assignee_total=entity.assignee_total,
            teams=[TeamCompletionType.from_domain(team) for team in entity.teams],
            team_total=entity.team_total,
            overdue=entity.overdue,
        )
