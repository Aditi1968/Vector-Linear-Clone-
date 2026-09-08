"""Everything analytics decides before a statement is issued.

The window arithmetic, the zero-filling, the ceiling on `days`, and the one
transport claim worth making without a database: that a refused window arrives
as a readable BAD_USER_INPUT rather than as "Internal server error".

`tests/test_analytics_db.py` is about the statements themselves.
"""

from datetime import date, datetime, timezone
from uuid import UUID

import pytest

from app.domain.analytics import (
    ANALYTICS_MAX_DAYS,
    ANALYTICS_MIN_DAYS,
    AssigneeLoad,
    CategoryCount,
    CycleTimeSummary,
    TeamCompletion,
    ThroughputDay,
    WorkspaceAnalytics,
    analytics_window,
    dense_throughput,
    window_bounds,
)
from app.domain.errors import ValidationError
from app.domain.estimates import EstimateScale
from app.domain.teams import WorkflowStateCategory
from app.repositories.analytics import AnalyticsRepository
from app.services.analytics import AnalyticsService

from tests.conftest import TEST_SCOPE, ExplodingPool


TODAY = date(2026, 3, 15)


# ------------------------------------------------------------------ the window


def test_a_one_day_window_starts_and_ends_today():
    assert analytics_window(1, TODAY) == (TODAY, TODAY)


def test_a_thirty_day_window_includes_today_as_its_last_day():
    start, end = analytics_window(30, TODAY)

    assert end == TODAY
    assert start == date(2026, 2, 14)
    # Thirty days INCLUSIVE of both ends, which is the count of buckets.
    assert (end - start).days + 1 == 30


def test_the_window_spans_a_year_boundary_without_arithmetic_of_its_own():
    assert analytics_window(90, date(2026, 1, 5))[0] == date(2025, 10, 8)


def test_the_bounds_are_half_open_utc_instants():
    start, end = window_bounds(date(2026, 3, 1), 3)

    assert start == datetime(2026, 3, 1, tzinfo=timezone.utc)
    # The instant AFTER the last day, so a completion at exactly midnight on
    # the 3rd falls in the 3rd's bucket and a completion at midnight on the 4th
    # falls outside the window entirely.
    assert end == datetime(2026, 3, 4, tzinfo=timezone.utc)


def test_every_bound_is_timezone_aware():
    start, end = window_bounds(TODAY, 7)

    # A naive datetime compared against a TIMESTAMPTZ column is interpreted in
    # the session's zone, which is the silent way this whole feature would
    # start reporting a different day's numbers per deployment.
    assert start.tzinfo is not None
    assert end.tzinfo is not None


# ------------------------------------------------------------- zero filling


def test_a_day_nothing_finished_on_is_a_zero_and_not_a_gap():
    days = dense_throughput({date(2026, 3, 2): (4, 1)}, date(2026, 3, 1), 3)

    assert days == [
        ThroughputDay(day=date(2026, 3, 1), completed=0, canceled=0),
        ThroughputDay(day=date(2026, 3, 2), completed=4, canceled=1),
        ThroughputDay(day=date(2026, 3, 3), completed=0, canceled=0),
    ]


def test_an_empty_window_is_all_zeros_and_still_the_right_length():
    days = dense_throughput({}, date(2026, 3, 1), 5)

    assert len(days) == 5
    assert all(day.completed == 0 and day.canceled == 0 for day in days)


def test_the_series_is_in_calendar_order():
    days = dense_throughput({}, date(2026, 2, 26), 6)

    assert [day.day for day in days] == [
        date(2026, 2, 26),
        date(2026, 2, 27),
        date(2026, 2, 28),
        date(2026, 3, 1),
        date(2026, 3, 2),
        date(2026, 3, 3),
    ]


def test_a_row_outside_the_window_is_dropped_rather_than_shifted():
    """A defensive claim about the filler, not about the SQL.

    The statement's own predicate already bounds the days it can return. If
    that ever stopped being true, silently placing a stray row at an end of the
    series would be the worst possible failure: a plausible spike on a day that
    did not have one.
    """
    days = dense_throughput(
        {date(2026, 1, 1): (99, 99), date(2026, 3, 2): (4, 0)},
        date(2026, 3, 1),
        3,
    )

    assert [day.completed for day in days] == [0, 4, 0]


# --------------------------------------------------------------- the ceiling


class _StubRepository(AnalyticsRepository):
    """Answers every statement without one, so the service is the subject."""

    def __init__(self):
        self.windows: list[tuple[datetime, datetime]] = []

    async def completion_series(
        self, connection, *, scope, start, end, range_start, days
    ):
        self.windows.append((start, end))

        return dense_throughput({}, range_start, days)

    async def cycle_time(self, connection, *, scope, start, end):
        self.windows.append((start, end))

        return None

    async def state_mix(self, connection, *, scope):
        return []

    async def workload(self, connection, *, scope, limit):
        return ([], 0)

    async def overdue(self, connection, *, scope, today):
        return 0

    async def team_completion(self, connection, *, scope, start, end, limit):
        self.windows.append((start, end))

        return ([], 0)


def _service() -> tuple[AnalyticsService, _StubRepository]:
    repository = _StubRepository()

    # `ExplodingPool` hands out a connection and fails on anything else, so a
    # service that reached for a second one -- or acquired per statement --
    # fails here rather than in production under load.
    return (AnalyticsService(pool=ExplodingPool(), repository=repository), repository)


@pytest.mark.parametrize("days", [0, -1, ANALYTICS_MAX_DAYS + 1, 365, 100000])
async def test_a_window_outside_the_bound_is_refused(days):
    service, _ = _service()

    with pytest.raises(ValidationError) as raised:
        await service.overview(scope=TEST_SCOPE, days=days)

    assert [issue.field for issue in raised.value.issues] == ["days"]


@pytest.mark.parametrize("days", [ANALYTICS_MIN_DAYS, 30, ANALYTICS_MAX_DAYS])
async def test_a_window_inside_the_bound_is_served(days):
    service, _ = _service()

    overview = await service.overview(scope=TEST_SCOPE, days=days)

    assert overview.days == days
    # One point per day, always -- which is what makes the ceiling a bound on
    # the response and not merely on the argument.
    assert len(overview.throughput) == days


async def test_an_oversized_window_is_never_quietly_clamped():
    """The failure this whole feature exists to avoid, in its smallest form.

    A clamped answer would come back with `days` reduced and every chart drawn
    over half the window the reader asked for, under a heading that still said
    a year.
    """
    service, _ = _service()

    with pytest.raises(ValidationError):
        await service.overview(scope=TEST_SCOPE, days=ANALYTICS_MAX_DAYS + 1)


async def test_every_statement_sees_the_same_window():
    """One reading of the clock, not one per statement.

    A request that straddles midnight would otherwise put the throughput series
    in one window and the team table in the next, and the two would disagree by
    a day's work with nothing on the screen to explain it.
    """
    service, repository = _service()

    await service.overview(scope=TEST_SCOPE, days=14)

    assert len(set(repository.windows)) == 1


# ------------------------------------------------------------------ transport


ANALYTICS_QUERY = """
query Analytics($slug: String!, $days: Int!) {
    workspaceAnalytics(workspaceSlug: $slug, days: $days) {
        days
        rangeStart
        rangeEnd
        overdue
        throughput { day completed canceled }
        cycleTime { count medianHours p90Hours }
        stateMix { category issues }
        workload { assigneeId name openIssues }
        assigneeTotal
        teams { teamId key name estimateScale completed estimated estimateTotal }
        teamTotal
    }
}
"""


class _FakeAnalyticsService:
    def __init__(self, overview=None, error=None):
        self._overview = overview
        self._error = error

    async def overview(self, *, scope, days):
        if self._error is not None:
            raise self._error

        return self._overview


def _overview() -> WorkspaceAnalytics:
    return WorkspaceAnalytics(
        range_start=date(2026, 3, 1),
        range_end=date(2026, 3, 2),
        days=2,
        throughput=[
            ThroughputDay(day=date(2026, 3, 1), completed=2, canceled=0),
            ThroughputDay(day=date(2026, 3, 2), completed=0, canceled=1),
        ],
        cycle_time=CycleTimeSummary(count=2, median_hours=36.0, p90_hours=72.0),
        state_mix=[
            CategoryCount(category=WorkflowStateCategory.STARTED, issues=3),
        ],
        workload=[
            AssigneeLoad(
                assignee_id=UUID("00000000-0000-7000-8000-0000000000fd"),
                name="Ada",
                open_issues=3,
            ),
            AssigneeLoad(assignee_id=None, name=None, open_issues=9),
        ],
        assignee_total=34,
        teams=[
            TeamCompletion(
                team_id=UUID("00000000-0000-7000-8000-0000000000fb"),
                key="CORE",
                name="Core",
                estimate_scale=EstimateScale.TSHIRT,
                completed=2,
                estimated=2,
                estimate_total=None,
            )
        ],
        team_total=1,
        overdue=5,
    )


async def _execute(schema, context, days=30):
    from tests.conftest import TEST_WORKSPACE_SLUG

    return await schema.execute(
        ANALYTICS_QUERY,
        variable_values={"slug": TEST_WORKSPACE_SLUG, "days": days},
        context_value=context,
    )


async def test_the_aggregate_reaches_the_client_intact(test_schema, graphql_context):
    context = graphql_context(analytics_service=_FakeAnalyticsService(_overview()))

    result = await _execute(test_schema, context)

    assert result.errors is None
    data = result.data["workspaceAnalytics"]
    assert data["days"] == 2
    assert data["overdue"] == 5
    assert data["throughput"] == [
        {"day": "2026-03-01", "completed": 2, "canceled": 0},
        {"day": "2026-03-02", "completed": 0, "canceled": 1},
    ]
    assert data["cycleTime"] == {"count": 2, "medianHours": 36.0, "p90Hours": 72.0}
    assert data["stateMix"] == [{"category": "STARTED", "issues": 3}]
    assert data["assigneeTotal"] == 34
    # The unassigned pile survives as a row rather than being dropped for
    # having no id: it is usually the largest bucket on the chart.
    assert data["workload"][1] == {"assigneeId": None, "name": None, "openIssues": 9}
    # A t-shirt team reports what it finished and refuses to sum the ladder.
    assert data["teams"][0]["estimateScale"] == "TSHIRT"
    assert data["teams"][0]["estimateTotal"] is None
    assert data["teams"][0]["estimated"] == 2


async def test_an_absent_cycle_time_is_null_and_not_a_summary_of_zeros(
    test_schema, graphql_context
):
    empty = WorkspaceAnalytics(
        range_start=date(2026, 3, 1),
        range_end=date(2026, 3, 1),
        days=1,
        throughput=[ThroughputDay(day=date(2026, 3, 1), completed=0, canceled=0)],
        cycle_time=None,
        state_mix=[],
        workload=[],
        assignee_total=0,
        teams=[],
        team_total=0,
        overdue=0,
    )
    context = graphql_context(analytics_service=_FakeAnalyticsService(empty))

    result = await _execute(test_schema, context)

    assert result.errors is None
    assert result.data["workspaceAnalytics"]["cycleTime"] is None


async def test_a_refused_window_is_readable_rather_than_masked(
    test_schema, graphql_context
):
    """The reason the resolver translates at all.

    A query field has no `errors` payload to put a rejection in, so its only
    channel is the top-level array -- which `app/graphql/schema.py` masks
    unless the error carries a published code. Without the translation the
    client would be told "Internal server error" for asking for 400 days.
    """
    from app.domain.errors import ValidationIssue

    refusal = ValidationError(
        [ValidationIssue(field="days", code="OUT_OF_RANGE", message="too wide")]
    )
    context = graphql_context(analytics_service=_FakeAnalyticsService(error=refusal))

    result = await _execute(test_schema, context, days=400)

    assert result.errors is not None
    assert result.errors[0].message == "Invalid analytics window"
    assert result.errors[0].extensions["code"] == "BAD_USER_INPUT"
    assert result.errors[0].extensions["issues"][0]["field"] == "days"


async def test_an_unexpected_failure_is_masked(test_schema, graphql_context):
    """A bug is not a validation error, however convenient that would be."""
    context = graphql_context(
        analytics_service=_FakeAnalyticsService(error=RuntimeError("boom"))
    )

    result = await _execute(test_schema, context)

    assert result.errors is not None
    assert result.errors[0].message == "Internal server error"
