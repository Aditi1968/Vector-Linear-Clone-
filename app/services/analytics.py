"""Business rules for the analytics screen: the window, and the ceiling on it.

The service owns connection acquisition and the transaction boundary, and the
workspace is threaded through every method rather than held on the instance,
for the reasons `IssueService` sets out at length. Holding a scope is not
permission to act in it.

There is one public method and it is a read. Nothing in this module writes.
"""

from datetime import datetime, timezone

import asyncpg

from app.domain.analytics import (
    ANALYTICS_MAX_DAYS,
    ANALYTICS_MIN_DAYS,
    TEAM_LIMIT,
    WORKLOAD_LIMIT,
    WorkspaceAnalytics,
    analytics_window,
    window_bounds,
)
from app.domain.errors import ValidationError, ValidationIssue
from app.domain.tenancy import WorkspaceScope
from app.repositories.analytics import AnalyticsRepository


class AnalyticsService:
    """Six aggregates over one workspace, for one bounded window."""

    def __init__(self, pool: asyncpg.Pool, repository: AnalyticsRepository):
        self._pool = pool
        self._repository = repository

    async def overview(
        self,
        *,
        scope: WorkspaceScope,
        days: int,
    ) -> WorkspaceAnalytics:
        """Everything the analytics page reads, in one call.

        THE BOUND, which is the reason this method validates at all.

        `days` is the only argument in this schema that sets the cardinality of
        a response without being a page size. `app/graphql/limits.py` prices a
        document during *validation*, from the page sizes written in it or
        declared on the field -- and `throughput` declares no `first`, because
        it is not a connection and has no cursor. So the complexity budget
        charges this field as one, whatever `days` says, and the ceiling has to
        live here instead. It is ANALYTICS_MAX_DAYS, it is 180, and the screen
        prints it beside the range picker.

        A value outside the range is REFUSED rather than clamped. Clamping
        would answer a request for a year with half a year of data under a
        heading that still said a year, which is precisely the class of quiet
        wrongness this feature was left unbuilt to avoid.

        `today` is read once, from the clock, and every one of the six
        statements is derived from that single reading. Asking the clock per
        statement would let a request that spans midnight put the throughput
        series in one window and the overdue count in the next.

        A single read transaction over all six, so that the statements see one
        snapshot instead of six. Under the default READ COMMITTED that is not a
        guarantee -- each statement still takes its own snapshot -- so this
        buys consistency of the connection rather than of the data.

        ponytail: no REPEATABLE READ. The visible symptom would be a team's
        completion count disagreeing with the throughput series by one issue
        finished mid-request, which is a race a person reloading the page
        cannot observe. `connection.transaction(isolation="repeatable_read")`
        is the upgrade, and it costs a snapshot held for six aggregates.
        """
        if not ANALYTICS_MIN_DAYS <= days <= ANALYTICS_MAX_DAYS:
            raise ValidationError(
                [
                    ValidationIssue(
                        field="days",
                        code="OUT_OF_RANGE",
                        message=(
                            f"days must be between {ANALYTICS_MIN_DAYS} and "
                            f"{ANALYTICS_MAX_DAYS}"
                        ),
                    )
                ]
            )

        today = datetime.now(timezone.utc).date()
        range_start, range_end = analytics_window(days, today)
        start, end = window_bounds(range_start, days)

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                throughput = await self._repository.completion_series(
                    connection,
                    scope=scope,
                    start=start,
                    end=end,
                    range_start=range_start,
                    days=days,
                )
                cycle_time = await self._repository.cycle_time(
                    connection,
                    scope=scope,
                    start=start,
                    end=end,
                )
                state_mix = await self._repository.state_mix(
                    connection,
                    scope=scope,
                )
                workload, assignee_total = await self._repository.workload(
                    connection,
                    scope=scope,
                    limit=WORKLOAD_LIMIT,
                )
                overdue = await self._repository.overdue(
                    connection,
                    scope=scope,
                    today=today,
                )
                teams, team_total = await self._repository.team_completion(
                    connection,
                    scope=scope,
                    start=start,
                    end=end,
                    limit=TEAM_LIMIT,
                )

        return WorkspaceAnalytics(
            range_start=range_start,
            range_end=range_end,
            days=days,
            throughput=throughput,
            cycle_time=cycle_time,
            state_mix=state_mix,
            workload=workload,
            assignee_total=assignee_total,
            teams=teams,
            team_total=team_total,
            overdue=overdue,
        )
