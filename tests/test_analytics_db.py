"""Every analytics statement, against a real PostgreSQL, over two tenants.

`tests/test_analytics.py` holds the window arithmetic, the zero-filling, the
ceiling and the transport. None of that touches a server, and none of it can
catch the failures this file exists for:

  * A STATEMENT THAT DOES NOT PARSE. mypy checks the Python around a query and
    nothing at all inside the string. Eleven aggregates went in as text.
  * A NUMBER THAT IS WRONG. `count(*) FILTER`, `percentile_cont`, a LATERAL
    join and a half-open range are each one character away from a plausible
    figure nobody can tell is wrong by looking at it.
  * A TENANT PREDICATE THAT LEAKS. This is the one that matters, and it has
    its own section at the foot of this file. An aggregate that forgets
    `workspace_id` does not return a visibly foreign row -- it returns one
    slightly larger integer, on a chart, with no way for a reader to notice.
    So workspace B is seeded LARGER than workspace A in every dimension, and
    every assertion about A is an exact equality: a dropped predicate cannot
    coincidentally still pass.

  * THE MIGRATION. 031 applied fresh, applied on top of a populated database,
    and applied twice.

Marked `db`: deselected by default, skipped when Docker is unreachable.
"""

from datetime import date, datetime, timezone
from uuid import UUID

import asyncpg
import pytest

from app.domain.analytics import (
    CYCLE_LIMIT,
    PROJECT_LIMIT,
    TEAM_LIMIT,
    WORKLOAD_LIMIT,
    throughput_totals,
)
from app.domain.estimates import EstimateScale
from app.domain.tenancy import WorkspaceScope
from app.repositories.analytics import AnalyticsRepository
from scripts.apply_migration import apply_migration

from tests.conftest import (
    MIGRATIONS_DIR,
    apply_all_migrations,
    reset_schema,
    seed_workflow_states,
)


pytestmark = pytest.mark.db


# The bootstrap tenant migrations/002_tenancy.sql seeds, reused rather than
# inserted: 005 gives workflow states to every team that exists when it runs,
# so this is the one team that already has a board.
WORKSPACE_A = UUID("00000000-0000-7000-8000-000000000001")
TEAM_A = UUID("00000000-0000-7000-8000-000000000002")

# A second team in A, on a DIFFERENT estimate scale. It is what makes the
# mixed-scale refusal testable: a workspace whose two teams estimate in points
# and in t-shirt sizes has no total, and the statement has to prove it does not
# produce one.
TEAM_A_TSHIRT = UUID("00000000-0000-7000-8000-00000000000a")

# The other tenant. Everything about it is deliberately BIGGER.
WORKSPACE_B = UUID("00000000-0000-7000-8000-0000000000b1")
TEAM_B = UUID("00000000-0000-7000-8000-0000000000b2")

SCOPE_A = WorkspaceScope(workspace_id=WORKSPACE_A)
SCOPE_B = WorkspaceScope(workspace_id=WORKSPACE_B)

ADA = UUID("00000000-0000-7000-8000-0000000000d1")
BOB = UUID("00000000-0000-7000-8000-0000000000d2")
CHRIS = UUID("00000000-0000-7000-8000-0000000000d3")

PROJECT_A = UUID("00000000-0000-7000-8000-0000000000e1")
PROJECT_B = UUID("00000000-0000-7000-8000-0000000000e2")

CYCLE_A = UUID("00000000-0000-7000-8000-0000000000f1")
CYCLE_B = UUID("00000000-0000-7000-8000-0000000000f2")
# A cycle in A that ended before the window opened. It exists to prove the
# overlap predicate excludes it, which is the difference between "the cycles
# this window is about" and "every cycle the team has ever run".
CYCLE_A_OLD = UUID("00000000-0000-7000-8000-0000000000f3")

# Shaped to satisfy `users_password_hash_argon2id` (003). Nothing here
# authenticates; the column is NOT NULL and constrained, so a seed needs a
# value the CHECK accepts and nothing more.
FAKE_HASH = "$argon2id$v=19$m=65536,t=3,p=4$c2VlZHNlZWQ$" + "0" * 43

# The window every assertion below is written against. Fixed rather than
# relative to the clock, because the subject here is the statements and a
# moving window would make the expected numbers move with the calendar.
#
# Ten days, [2026-03-01, 2026-03-11), half-open exactly as `window_bounds`
# builds it -- so an issue completed at midnight on the 11th is outside.
RANGE_START = date(2026, 3, 1)
WINDOW_DAYS = 10
WINDOW_START = datetime(2026, 3, 1, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 3, 11, tzinfo=timezone.utc)

# The `today` passed to `overdue`, inside the window. A parameter rather than
# the server's clock, so "is this issue late" has a stable answer.
TODAY = date(2026, 3, 10)


def _at(day: int, hour: int = 0) -> datetime:
    return datetime(2026, 3, day, hour, tzinfo=timezone.utc)


def issue_id(index: int) -> UUID:
    return UUID(f"a1b2c3d4-0000-4000-9000-{index:012d}")


# (index, workspace, team, category, created_at, completed_at, priority,
#  assignee, project, cycle, estimate, due_date)
#
# Read this table as the answer key: every expected number in every test below
# is countable off these rows by hand, which is the only way an aggregate test
# is worth anything. An expectation derived by running the code it tests
# asserts that the code does what it does.
ISSUE_SEED = (
    # --- workspace A, team A (points) --------------------------------------
    # Completed inside the window. 48h and 36h from creation, so the lead-time
    # median over the three completions is exactly 48.
    (
        1,
        WORKSPACE_A,
        TEAM_A,
        "completed",
        _at(1),
        _at(3),
        1,
        ADA,
        PROJECT_A,
        CYCLE_A,
        3,
        None,
    ),
    (
        2,
        WORKSPACE_A,
        TEAM_A,
        "completed",
        _at(2),
        _at(3, 12),
        2,
        ADA,
        PROJECT_A,
        CYCLE_A,
        5,
        None,
    ),
    # Canceled inside the window: carries a completed_at exactly as a delivery
    # does, and must never be counted as one.
    (
        3,
        WORKSPACE_A,
        TEAM_A,
        "canceled",
        _at(1),
        _at(5),
        0,
        None,
        PROJECT_A,
        CYCLE_A,
        13,
        None,
    ),
    # Open, overdue, assigned, in the project and the cycle.
    (
        4,
        WORKSPACE_A,
        TEAM_A,
        "started",
        datetime(2026, 2, 1, tzinfo=timezone.utc),
        None,
        1,
        ADA,
        PROJECT_A,
        CYCLE_A,
        2,
        date(2026, 3, 5),
    ),
    # Open, unassigned, in nothing. The unassigned pile is a real bucket.
    (
        5,
        WORKSPACE_A,
        TEAM_A,
        "backlog",
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        None,
        0,
        None,
        None,
        None,
        None,
        None,
    ),
    # Completed BEFORE the window opened. In `state_mix` (a snapshot of now)
    # and out of everything with a range predicate.
    (
        6,
        WORKSPACE_A,
        TEAM_A,
        "completed",
        datetime(2026, 2, 1, tzinfo=timezone.utc),
        datetime(2026, 2, 15, tzinfo=timezone.utc),
        3,
        BOB,
        None,
        None,
        8,
        None,
    ),
    # --- workspace A, team A_TSHIRT (tshirt) --------------------------------
    # 48h, so it is the third sample in the lead-time median.
    (
        7,
        WORKSPACE_A,
        TEAM_A_TSHIRT,
        "completed",
        _at(4),
        _at(6),
        4,
        BOB,
        None,
        None,
        3,
        None,
    ),
    # In LAST cycle, finished in February. It exists so that dropping the
    # overlap predicate would return two cycles instead of one: the cycle
    # statement reads FROM issues, so an empty old cycle would be excluded by
    # the join alone and would prove nothing about the predicate.
    (
        8,
        WORKSPACE_A,
        TEAM_A,
        "completed",
        datetime(2026, 1, 20, tzinfo=timezone.utc),
        datetime(2026, 2, 10, tzinfo=timezone.utc),
        2,
        ADA,
        None,
        CYCLE_A_OLD,
        1,
        None,
    ),
    # --- workspace B (hours) -- deliberately larger in every dimension ------
    (
        101,
        WORKSPACE_B,
        TEAM_B,
        "completed",
        _at(1),
        _at(2),
        4,
        CHRIS,
        PROJECT_B,
        CYCLE_B,
        40,
        None,
    ),
    (
        102,
        WORKSPACE_B,
        TEAM_B,
        "completed",
        _at(1),
        _at(2),
        4,
        CHRIS,
        PROJECT_B,
        CYCLE_B,
        40,
        None,
    ),
    (
        103,
        WORKSPACE_B,
        TEAM_B,
        "completed",
        _at(1),
        _at(2),
        4,
        CHRIS,
        PROJECT_B,
        CYCLE_B,
        40,
        None,
    ),
    (
        104,
        WORKSPACE_B,
        TEAM_B,
        "completed",
        _at(1),
        _at(2),
        4,
        CHRIS,
        PROJECT_B,
        CYCLE_B,
        40,
        None,
    ),
    (
        105,
        WORKSPACE_B,
        TEAM_B,
        "completed",
        _at(1),
        _at(2),
        4,
        CHRIS,
        PROJECT_B,
        CYCLE_B,
        40,
        None,
    ),
    (
        106,
        WORKSPACE_B,
        TEAM_B,
        "canceled",
        _at(1),
        _at(4),
        3,
        CHRIS,
        PROJECT_B,
        CYCLE_B,
        40,
        None,
    ),
    (
        107,
        WORKSPACE_B,
        TEAM_B,
        "canceled",
        _at(1),
        _at(4),
        3,
        CHRIS,
        PROJECT_B,
        CYCLE_B,
        40,
        None,
    ),
    (
        108,
        WORKSPACE_B,
        TEAM_B,
        "started",
        _at(1),
        None,
        2,
        CHRIS,
        PROJECT_B,
        CYCLE_B,
        40,
        date(2026, 3, 1),
    ),
    (
        109,
        WORKSPACE_B,
        TEAM_B,
        "started",
        _at(1),
        None,
        2,
        CHRIS,
        PROJECT_B,
        CYCLE_B,
        40,
        date(2026, 3, 1),
    ),
    (
        110,
        WORKSPACE_B,
        TEAM_B,
        "backlog",
        _at(1),
        None,
        1,
        None,
        PROJECT_B,
        CYCLE_B,
        40,
        date(2026, 3, 1),
    ),
)

# Which issues got a recorded transition into a `started` state, and when.
#
# Only two of A's three window completions, ON PURPOSE. That is the coverage
# gap cycle time is published with: #7 was dragged straight to Done and has no
# start to measure from, so the honest report is "2 of 3" rather than a median
# that looks like it covers everything.
STARTED_SEED = (
    (1, WORKSPACE_A, TEAM_A, _at(2)),  # started 24h before completion
    (2, WORKSPACE_A, TEAM_A, _at(3)),  # started 12h before completion
    (101, WORKSPACE_B, TEAM_B, _at(1, 12)),
)

INSERT_ISSUE_SQL = """
INSERT INTO issues (
    id, workspace_id, team_id, number, workflow_state_id, title,
    created_at, completed_at, priority, assignee_id, project_id, cycle_id,
    estimate, due_date
)
VALUES (
    $1, $2, $3, $4,
    (
        SELECT id FROM workflow_states
        WHERE workspace_id = $2 AND team_id = $3 AND type = $5
        ORDER BY position
        LIMIT 1
    ),
    $6, $7, $8, $9, $10, $11, $12, $13, $14
)
"""

# A `state_changed` row exactly as `ActivityService` writes one: the
# workflow-state ids as TEXT in from_value/to_value. `to_value` is what the
# cycle-time lateral resolves back to a category.
INSERT_STARTED_SQL = """
INSERT INTO issue_activity (
    workspace_id, issue_id, kind, from_value, to_value, created_at
)
VALUES (
    $1, $2, 'state_changed',
    (
        SELECT id::text FROM workflow_states
        WHERE workspace_id = $1 AND team_id = $3 AND type = 'backlog'
        LIMIT 1
    ),
    (
        SELECT id::text FROM workflow_states
        WHERE workspace_id = $1 AND team_id = $3 AND type = 'started'
        LIMIT 1
    ),
    $4
)
"""


async def _seed(connection) -> None:
    """The two tenants, from an empty database up.

    Every migration first, so the schema under test is the one 031 leaves
    behind rather than a prefix of it.
    """
    await reset_schema(connection)
    await apply_all_migrations(connection)

    await connection.executemany(
        "INSERT INTO users (id, email, password_hash, name) VALUES ($1, $2, $3, $4)",
        [
            (ADA, "ada@vector.test", FAKE_HASH, "Ada"),
            # No name, so the workload statement's fallback to email is
            # exercised rather than assumed.
            (BOB, "bob@vector.test", FAKE_HASH, None),
            (CHRIS, "chris@vector.test", FAKE_HASH, "Chris"),
        ],
    )

    await connection.execute(
        "INSERT INTO workspaces (id, slug, name) VALUES ($1, 'other', 'Other')",
        WORKSPACE_B,
    )
    await connection.executemany(
        "INSERT INTO teams (id, workspace_id, name, key, estimate_scale) "
        "VALUES ($1, $2, $3, $4, $5)",
        [
            (TEAM_A_TSHIRT, WORKSPACE_A, "Design", "DES", "tshirt"),
            (TEAM_B, WORKSPACE_B, "Other Core", "OTH", "hours"),
        ],
    )
    # The bootstrap team predates 029, so it is on the default scale. Put it on
    # points explicitly: a workspace where every team is on 'none' would make
    # the mixed-scale assertions vacuous.
    await connection.execute(
        "UPDATE teams SET estimate_scale = 'points' WHERE id = $1", TEAM_A
    )

    for workspace, team in ((WORKSPACE_A, TEAM_A_TSHIRT), (WORKSPACE_B, TEAM_B)):
        await seed_workflow_states(connection, workspace, team)

    # Assignees must be members: `issues_assignee_fk` is composite over
    # (workspace_id, assignee_id) onto `workspace_members`, so this is the
    # database refusing an assignee from another tenant rather than a service.
    await connection.executemany(
        "INSERT INTO workspace_members (workspace_id, user_id, role) "
        "VALUES ($1, $2, $3)",
        [
            (WORKSPACE_A, ADA, "member"),
            (WORKSPACE_A, BOB, "member"),
            (WORKSPACE_B, CHRIS, "member"),
        ],
    )

    await connection.executemany(
        "INSERT INTO projects (id, workspace_id, name, state) VALUES ($1, $2, $3, $4)",
        [
            (PROJECT_A, WORKSPACE_A, "Launch", "started"),
            (PROJECT_B, WORKSPACE_B, "Other Launch", "planned"),
        ],
    )

    await connection.executemany(
        "INSERT INTO cycles (id, workspace_id, team_id, number, name, "
        "starts_at, ends_at) VALUES ($1, $2, $3, $4, $5, $6, $7)",
        [
            (CYCLE_A, WORKSPACE_A, TEAM_A, 7, "Sprint 7", _at(1), _at(15)),
            (CYCLE_B, WORKSPACE_B, TEAM_B, 3, None, _at(1), _at(15)),
            # Ends before the window opens, so the overlap predicate drops it.
            (
                CYCLE_A_OLD,
                WORKSPACE_A,
                TEAM_A,
                6,
                "Sprint 6",
                datetime(2026, 2, 1, tzinfo=timezone.utc),
                datetime(2026, 2, 14, tzinfo=timezone.utc),
            ),
        ],
    )

    await connection.executemany(
        INSERT_ISSUE_SQL,
        [
            (
                issue_id(index),
                workspace,
                team,
                index,
                category,
                f"Issue {index}",
                created,
                completed,
                priority,
                assignee,
                project,
                cycle,
                estimate,
                due,
            )
            for (
                index,
                workspace,
                team,
                category,
                created,
                completed,
                priority,
                assignee,
                project,
                cycle,
                estimate,
                due,
            ) in ISSUE_SEED
        ],
    )

    await connection.executemany(
        INSERT_STARTED_SQL,
        [
            (workspace, issue_id(index), team, moment)
            for index, workspace, team, moment in STARTED_SEED
        ],
    )


@pytest.fixture
async def connection(postgres_dsn):
    established = await asyncpg.connect(postgres_dsn)

    try:
        await _seed(established)

        yield established
    finally:
        await established.close()


@pytest.fixture
def repository() -> AnalyticsRepository:
    return AnalyticsRepository()


async def _overview(repository, connection, scope):
    """Every statement, for one scope, exactly as the service issues them.

    A helper rather than eleven calls per test, because the cross-workspace
    section below has to run all eleven twice and compare -- and a test that
    listed them by hand would be a test that quietly stopped covering the
    twelfth.
    """
    created = await repository.creation_series(
        connection, scope=scope, start=WINDOW_START, end=WINDOW_END
    )

    return {
        "throughput": await repository.completion_series(
            connection,
            scope=scope,
            start=WINDOW_START,
            end=WINDOW_END,
            range_start=RANGE_START,
            days=WINDOW_DAYS,
            created=created,
        ),
        "lead_time": await repository.lead_time(
            connection, scope=scope, start=WINDOW_START, end=WINDOW_END
        ),
        "cycle_time": await repository.cycle_time(
            connection, scope=scope, start=WINDOW_START, end=WINDOW_END
        ),
        "issue_age": await repository.issue_age(connection, scope=scope),
        "state_mix": await repository.state_mix(connection, scope=scope),
        "priority_mix": await repository.priority_mix(connection, scope=scope),
        "workload": await repository.workload(
            connection, scope=scope, limit=WORKLOAD_LIMIT
        ),
        "overdue": await repository.overdue(connection, scope=scope, today=TODAY),
        "teams": await repository.team_completion(
            connection,
            scope=scope,
            start=WINDOW_START,
            end=WINDOW_END,
            limit=TEAM_LIMIT,
        ),
        "projects": await repository.project_progress(
            connection, scope=scope, limit=PROJECT_LIMIT
        ),
        "cycles": await repository.cycle_progress(
            connection,
            scope=scope,
            start=WINDOW_START,
            end=WINDOW_END,
            limit=CYCLE_LIMIT,
        ),
    }


# --------------------------------------------------------------- throughput


async def test_the_two_series_bucket_on_two_different_columns(repository, connection):
    """Filing and finishing are different events on different days.

    #1 is filed on the 1st and finished on the 3rd, so it is on two bars. A
    single statement counting both would have had to pick one.
    """
    created = await repository.creation_series(
        connection, scope=SCOPE_A, start=WINDOW_START, end=WINDOW_END
    )
    series = await repository.completion_series(
        connection,
        scope=SCOPE_A,
        start=WINDOW_START,
        end=WINDOW_END,
        range_start=RANGE_START,
        days=WINDOW_DAYS,
        created=created,
    )

    by_day = {day.day: (day.created, day.completed, day.canceled) for day in series}

    assert len(series) == WINDOW_DAYS, "one bucket per day, filled"
    assert by_day[date(2026, 3, 1)] == (2, 0, 0), "#1 and #3 filed"
    assert by_day[date(2026, 3, 2)] == (1, 0, 0), "#2 filed"
    assert by_day[date(2026, 3, 3)] == (0, 2, 0), "#1 and #2 delivered"
    assert by_day[date(2026, 3, 4)] == (1, 0, 0), "#7 filed"
    # The cancellation carries a completed_at exactly as a delivery does, and
    # lands in its own column rather than in `completed`.
    assert by_day[date(2026, 3, 5)] == (0, 0, 1)
    assert by_day[date(2026, 3, 6)] == (0, 1, 0), "#7 delivered"
    assert by_day[date(2026, 3, 7)] == (0, 0, 0), "a real day with nothing on it"


async def test_the_completion_rate_is_over_what_stopped(repository, connection):
    created = await repository.creation_series(
        connection, scope=SCOPE_A, start=WINDOW_START, end=WINDOW_END
    )
    series = await repository.completion_series(
        connection,
        scope=SCOPE_A,
        start=WINDOW_START,
        end=WINDOW_END,
        range_start=RANGE_START,
        days=WINDOW_DAYS,
        created=created,
    )

    totals = throughput_totals(series)

    assert (totals.created, totals.completed, totals.canceled) == (4, 3, 1)
    # 3 delivered of the 4 that stopped -- NOT 3 of the 4 filed, which is the
    # same number here by coincidence and would not be next month.
    assert totals.completion_rate == 0.75


async def test_work_that_stopped_outside_the_window_is_outside_every_range(
    repository, connection
):
    """#6 was delivered in February. The half-open predicate excludes it."""
    lead_time = await repository.lead_time(
        connection, scope=SCOPE_A, start=WINDOW_START, end=WINDOW_END
    )

    assert lead_time is not None
    assert lead_time.count == 3, "#1, #2 and #7 -- not #6"


# ----------------------------------------------------------- the durations


async def test_lead_time_measures_creation_to_completion(repository, connection):
    """48h, 36h and 48h. The median is the middle one after sorting."""
    lead_time = await repository.lead_time(
        connection, scope=SCOPE_A, start=WINDOW_START, end=WINDOW_END
    )

    assert lead_time is not None
    assert lead_time.count == 3
    assert lead_time.median_hours == 48.0
    assert lead_time.p90_hours == 48.0


async def test_cycle_time_reports_its_coverage_and_not_just_its_median(
    repository, connection
):
    """The claim this metric exists to make.

    Two of A's three window completions have a recorded start; #7 went straight
    to Done and has none. The summary carries both numbers, so the screen can
    say "2 of 3" instead of printing a median that looks like it covers all
    three -- which it does not, and which would be biased towards exactly the
    work that was tracked most carefully.
    """
    cycle_time = await repository.cycle_time(
        connection, scope=SCOPE_A, start=WINDOW_START, end=WINDOW_END
    )

    assert cycle_time is not None
    assert cycle_time.measured == 2
    assert cycle_time.completed_total == 3
    # #1 started on the 2nd and finished on the 3rd (24h); #2 started on the 3rd
    # at 00:00 and finished at 12:00 (12h). Interpolated median of two: 18.
    assert cycle_time.median_hours == 18.0


async def test_cycle_time_is_shorter_than_lead_time_for_the_same_issues(
    repository, connection
):
    """A sanity claim that would catch the two being swapped.

    Work cannot spend longer in progress than it spent existing, so if these
    two ever cross, the LATERAL is resolving the wrong instant.
    """
    lead_time = await repository.lead_time(
        connection, scope=SCOPE_A, start=WINDOW_START, end=WINDOW_END
    )
    cycle_time = await repository.cycle_time(
        connection, scope=SCOPE_A, start=WINDOW_START, end=WINDOW_END
    )

    assert lead_time is not None and cycle_time is not None
    assert cycle_time.median_hours < lead_time.median_hours


async def test_an_activity_row_that_is_not_a_state_change_cannot_break_the_cast(
    repository, connection
):
    """The reason the lateral joins on `id::text` and not `to_value::uuid`.

    `to_value` is opaque TEXT whose meaning depends on `kind`: a
    `title_changed` row holds a title. Casting that column to UUID is an
    expression the planner may evaluate before the `kind` filter has excluded
    the row, and the failure is a 22P02 raised out of an aggregate -- for
    whichever workspace happened to have retitled an issue.
    """
    await connection.execute(
        """
        INSERT INTO issue_activity (workspace_id, issue_id, kind, to_value)
        VALUES ($1, $2, 'title_changed', 'Not a UUID at all')
        """,
        WORKSPACE_A,
        issue_id(1),
    )

    cycle_time = await repository.cycle_time(
        connection, scope=SCOPE_A, start=WINDOW_START, end=WINDOW_END
    )

    assert cycle_time is not None
    assert cycle_time.measured == 2, "the retitle changed nothing"


async def test_issue_age_is_over_the_unfinished_work_only(repository, connection):
    """#4 and #5. Not the four completed issues, whose duration is lead time."""
    age = await repository.issue_age(connection, scope=SCOPE_A)

    assert age is not None
    assert age.count == 2
    # Seeded in January and February against a real `now()`, so the hours move
    # with the calendar and only the ordering is worth pinning.
    assert age.p90_hours >= age.median_hours > 0


# --------------------------------------------------------- the distributions


async def test_state_mix_is_every_live_issue_including_the_finished_ones(
    repository, connection
):
    """A snapshot of the board, so completed work is on it until it is filed."""
    mix = {
        count.category: count.issues
        for count in await repository.state_mix(connection, scope=SCOPE_A)
    }

    assert mix == {"backlog": 1, "started": 1, "completed": 5, "canceled": 1}
    assert sum(mix.values()) == 8, "every issue in A, and none from B"


async def test_priority_mix_is_the_unfinished_work_and_zero_is_a_level(
    repository, connection
):
    """A different population from `state_mix`, and the same one as `workload`.

    0 is "no priority" rather than the lowest, and it is a bucket rather than
    an exclusion -- most issues are in it.
    """
    mix = {
        count.priority: count.issues
        for count in await repository.priority_mix(connection, scope=SCOPE_A)
    }

    assert mix == {0: 1, 1: 1}, "#5 at none and #4 at urgent; the rest are done"


async def test_workload_keeps_the_unassigned_pile_and_names_the_rest(
    repository, connection
):
    loads, total = await repository.workload(
        connection, scope=SCOPE_A, limit=WORKLOAD_LIMIT
    )

    assert total == 2, "one named assignee and the unassigned pile"
    assert {(load.assignee_id, load.name, load.open_issues) for load in loads} == {
        (ADA, "Ada", 1),
        (None, None, 1),
    }


async def test_a_member_with_no_name_falls_back_to_their_email(repository, connection):
    """Bob has no `name`. A blank label on a bar chart is a rendering bug that
    looks like missing data, so the statement coalesces rather than returning
    NULL for a person who exists."""
    # Move an open issue onto Bob so he holds live work at all.
    await connection.execute(
        "UPDATE issues SET assignee_id = $1 WHERE id = $2", BOB, issue_id(5)
    )

    loads, _ = await repository.workload(
        connection, scope=SCOPE_A, limit=WORKLOAD_LIMIT
    )

    assert {load.name for load in loads} == {"Ada", "bob@vector.test"}


async def test_a_removed_member_still_appears_in_the_workload(repository, connection):
    """A DECISION, recorded as a test because it is not self-evident.

    026 tombstones a membership with `removed_at` rather than deleting the row.
    The workload statement joins `users` and not `workspace_members`, so a
    departed member holding unfinished work is still shown holding it.

    That is deliberate. The chart answers "who is this work waiting on", and
    work assigned to somebody who has left is the single most useful thing on
    it: dropping them would silently shrink the total and make the backlog look
    owned when it is not. The screen labels the population as assignees rather
    than as members for exactly this reason.
    """
    await connection.execute(
        "UPDATE workspace_members SET removed_at = now() "
        "WHERE workspace_id = $1 AND user_id = $2",
        WORKSPACE_A,
        ADA,
    )

    loads, total = await repository.workload(
        connection, scope=SCOPE_A, limit=WORKLOAD_LIMIT
    )

    assert total == 2
    assert ADA in {load.assignee_id for load in loads}


async def test_overdue_counts_the_late_and_not_the_due_today(repository, connection):
    """#4 is due on the 5th and today is the 10th."""
    assert await repository.overdue(connection, scope=SCOPE_A, today=TODAY) == 1

    # A thing due today is not yet late, which is the boundary `<` encodes.
    assert (
        await repository.overdue(connection, scope=SCOPE_A, today=date(2026, 3, 5)) == 0
    )


# ------------------------------------------------------ the estimate scales


async def test_estimates_are_summed_within_a_team_and_never_across_two(
    repository, connection
):
    """The mixed-scale rule, enforced by the statement rather than the caller.

    A on points delivered 3 + 5 = 8. DES on t-shirt delivered one issue whose
    stored 3 is a rung on a ladder, so it has no total at all. There is no row
    here holding 11, and there is no way to write one: `estimate_scale` is in
    the GROUP BY.
    """
    teams, total = await repository.team_completion(
        connection,
        scope=SCOPE_A,
        start=WINDOW_START,
        end=WINDOW_END,
        limit=TEAM_LIMIT,
    )

    assert total == 2
    by_key = {team.key: team for team in teams}

    assert by_key["CORE"].estimate_scale is EstimateScale.POINTS
    assert by_key["CORE"].completed == 2
    assert by_key["CORE"].estimate_total == 8

    assert by_key["DES"].estimate_scale is EstimateScale.TSHIRT
    assert by_key["DES"].completed == 1
    # Sized, but not summable. The two facts are separate fields precisely so
    # a client can tell "nobody estimated it" from "the unit does not add up".
    assert by_key["DES"].estimated == 1
    assert by_key["DES"].estimate_total is None


# ------------------------------------------------------------ the breakdowns


async def test_project_progress_counts_live_issues_and_the_done_ones(
    repository, connection
):
    projects, total = await repository.project_progress(
        connection, scope=SCOPE_A, limit=PROJECT_LIMIT
    )

    assert total == 1
    assert (projects[0].name, projects[0].state) == ("Launch", "started")
    # #1, #2, #3 and #4 are in it; two of them are delivered. #3 is canceled
    # and counts towards the total without counting as done.
    assert (projects[0].issues, projects[0].completed) == (4, 2)


async def test_archiving_an_issue_moves_it_out_of_both_halves_of_the_ratio(
    repository, connection
):
    """Progress must not RISE because somebody filed a completed issue away,
    and must not FALL because they filed an open one. Both counts come from one
    population, so archiving moves the row out of the numerator and the
    denominator together."""
    await connection.execute(
        "UPDATE issues SET archived_at = now() WHERE id = $1", issue_id(1)
    )

    projects, _ = await repository.project_progress(
        connection, scope=SCOPE_A, limit=PROJECT_LIMIT
    )

    assert (projects[0].issues, projects[0].completed) == (3, 1)


async def test_only_the_cycles_the_window_overlaps_are_returned(repository, connection):
    """Sprint 6 ended in February, before the window opened.

    It holds a real issue (#8), so it is reachable through the join and is
    excluded by the overlap predicate alone. Without that predicate this
    returns two rows.
    """
    cycles, total = await repository.cycle_progress(
        connection,
        scope=SCOPE_A,
        start=WINDOW_START,
        end=WINDOW_END,
        limit=CYCLE_LIMIT,
    )

    assert total == 1
    assert cycles[0].cycle_id == CYCLE_A
    assert cycles[0].number == 7
    assert CYCLE_A_OLD not in {cycle.cycle_id for cycle in cycles}


async def test_a_cycle_reports_velocity_and_progress_from_one_scan(
    repository, connection
):
    """The same two counts, read for two reasons.

    Sprint 7 holds #1, #2, #3 and #4. Two are delivered, and those two carry
    estimates of 3 and 5 -- so 8 points of velocity against 4 issues of scope.
    #3's estimate of 13 is NOT in the total: it was canceled, and counting
    abandoned work as delivered is the number this whole feature is written to
    not produce.
    """
    cycles, _ = await repository.cycle_progress(
        connection,
        scope=SCOPE_A,
        start=WINDOW_START,
        end=WINDOW_END,
        limit=CYCLE_LIMIT,
    )

    assert (cycles[0].issues, cycles[0].completed) == (4, 2)
    assert cycles[0].estimated == 2
    assert cycles[0].completed_estimate == 8
    assert cycles[0].estimate_scale is EstimateScale.POINTS
    assert cycles[0].team_key == "CORE"


# ===========================================================================
# CROSS-WORKSPACE
# ===========================================================================
#
# The section that matters most. Everything above would still pass if every
# `workspace_id = $1` were deleted from every statement -- there would simply
# be more rows in the answers, and an aggregate does not show its rows.
#
# So these are written to FAIL on a dropped predicate specifically. Workspace B
# holds ten issues to A's seven, five deliveries to A's three, forty-point
# estimates to A's single digits, and its own project, cycle, team and
# assignee. Every assertion below is an exact equality against A's numbers
# alone, and every one of them changes if B's rows are visible.


async def test_no_aggregate_over_one_workspace_can_see_the_other(
    repository, connection
):
    """Every statement, both tenants, and no number shared between them.

    Run as one test over the whole set rather than eleven tests over one
    statement each, because the failure being guarded against is a predicate
    missing from ONE of them -- and a test per statement is a test somebody
    forgets to add when the twelfth lands. `_overview` is what the service
    calls; if it grows a statement, this grows with it.
    """
    a = await _overview(repository, connection, SCOPE_A)
    b = await _overview(repository, connection, SCOPE_B)

    # Throughput: all three series, and all three deliberately.
    #
    # `completed` alone was not enough, and a mutation run proved it: dropping
    # the tenant predicate from `creation_series` left every assertion in this
    # test green, because creation is bucketed by a different statement over a
    # different column and nothing here read it. A leak is per statement, so
    # the assertions have to be per statement too.
    #
    # A filed 4 and finished 3; B filed 10 and finished 5. Every number changes
    # if the other tenant is visible.
    assert throughput_totals(a["throughput"]).created == 4
    assert throughput_totals(b["throughput"]).created == 10
    assert throughput_totals(a["throughput"]).completed == 3
    assert throughput_totals(b["throughput"]).completed == 5
    assert throughput_totals(a["throughput"]).canceled == 1
    assert throughput_totals(b["throughput"]).canceled == 2

    # Durations: B's issues were delivered in 24h flat, A's in 36-48h, so a
    # leak moves both medians rather than merely one count.
    assert a["lead_time"].count == 3
    assert b["lead_time"].count == 5
    assert a["lead_time"].median_hours == 48.0
    assert b["lead_time"].median_hours == 24.0

    assert (a["cycle_time"].measured, a["cycle_time"].completed_total) == (2, 3)
    assert (b["cycle_time"].measured, b["cycle_time"].completed_total) == (1, 5)

    # Snapshots: A holds 8 live issues, B holds 10.
    assert sum(count.issues for count in a["state_mix"]) == 8
    assert sum(count.issues for count in b["state_mix"]) == 10
    assert sum(count.issues for count in a["priority_mix"]) == 2
    assert sum(count.issues for count in b["priority_mix"]) == 3

    assert a["issue_age"].count == 2
    assert b["issue_age"].count == 3

    # Workload: A has Ada and the unassigned pile; B has Chris and its own.
    assert a["workload"][1] == 2
    assert b["workload"][1] == 2
    assert {load.assignee_id for load in a["workload"][0]} == {ADA, None}
    assert {load.assignee_id for load in b["workload"][0]} == {CHRIS, None}

    # Overdue: A has one late issue, B has three.
    assert a["overdue"] == 1
    assert b["overdue"] == 3

    # Teams, projects and cycles: no id, key or name crosses.
    assert {team.key for team in a["teams"][0]} == {"CORE", "DES"}
    assert {team.key for team in b["teams"][0]} == {"OTH"}
    assert (a["teams"][1], b["teams"][1]) == (2, 1)

    assert [project.project_id for project in a["projects"][0]] == [PROJECT_A]
    assert [project.project_id for project in b["projects"][0]] == [PROJECT_B]
    assert (a["projects"][1], b["projects"][1]) == (1, 1)

    assert [cycle.cycle_id for cycle in a["cycles"][0]] == [CYCLE_A]
    assert [cycle.cycle_id for cycle in b["cycles"][0]] == [CYCLE_B]
    assert (a["cycles"][1], b["cycles"][1]) == (1, 1)


async def test_a_workspace_with_nothing_in_it_reads_as_empty_and_not_as_everyone(
    repository, connection
):
    """The purest form of the leak, and the one a screen would show loudest.

    A tenant that has created nothing must see zeros. If any predicate were
    missing, this scope would render the OTHER two workspaces' numbers under
    its own name -- and every one of them is a plausible figure for a workspace
    that had been used.
    """
    empty = UUID("00000000-0000-7000-8000-0000000000c9")
    await connection.execute(
        "INSERT INTO workspaces (id, slug, name) VALUES ($1, 'empty', 'Empty')",
        empty,
    )

    result = await _overview(repository, connection, WorkspaceScope(workspace_id=empty))

    totals = throughput_totals(result["throughput"])

    assert (totals.created, totals.completed, totals.canceled) == (0, 0, 0)
    assert totals.completion_rate is None
    # Absent, not zero: an empty summary and a real one that happens to be fast
    # render identically, and only one of them is a fact about a team.
    assert result["lead_time"] is None
    assert result["cycle_time"] is None
    assert result["issue_age"] is None
    assert result["state_mix"] == []
    assert result["priority_mix"] == []
    assert result["overdue"] == 0
    assert result["workload"] == ([], 0)
    assert result["teams"] == ([], 0)
    assert result["projects"] == ([], 0)
    assert result["cycles"] == ([], 0)

    # And the series is still the right SHAPE: ten days of zeros, not no days.
    # A chart handed an empty array draws nothing and looks like a bug; one
    # handed ten zeros says "nothing happened", which is the truth.
    assert len(result["throughput"]) == WINDOW_DAYS


# ===========================================================================
# MIGRATION 031
# ===========================================================================


async def test_the_index_exists_after_a_fresh_run_of_every_migration(connection):
    """001 through 031 in order, on an empty database.

    `connection` has already applied all of them, so this reads the result. The
    index is partial and its predicate is the point rather than a saving: an
    open issue can never satisfy a `completed_at` range, so its entry would be
    dead weight in every scan and write amplification on every insert.
    """
    definition = await connection.fetchval(
        "SELECT indexdef FROM pg_indexes WHERE indexname = $1",
        "issues_workspace_completed_at_idx",
    )

    assert definition is not None, "031 did not create its index"
    assert "workspace_id" in definition
    assert "completed_at" in definition
    assert "WHERE (completed_at IS NOT NULL)" in definition


async def test_031_applies_on_top_of_a_populated_database(postgres_dsn):
    """THE UPGRADE PATH, which a fresh run does not exercise.

    A fresh database has no rows, so `CREATE INDEX` over it is trivially
    successful whatever the index says. The failure this guards against is an
    index that cannot be built over data that already exists -- a predicate
    referencing a column added later, a uniqueness claim the existing rows
    violate.

    So: every migration EXCEPT 031, then real issues in both terminal states
    and both tenants, then 031 on top.
    """
    established = await asyncpg.connect(postgres_dsn)

    try:
        await reset_schema(established)

        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name == "031_analytics.sql":
                continue

            async with established.transaction():
                await apply_migration(established, path, migrations_dir=MIGRATIONS_DIR)

        # Rows first, and rows that the index actually has to sort: two with a
        # completed_at and one without, so both sides of the partial predicate
        # are populated before the index is built over them.
        await established.executemany(
            INSERT_ISSUE_SQL,
            [
                (
                    issue_id(index),
                    WORKSPACE_A,
                    TEAM_A,
                    index,
                    category,
                    f"Pre-existing {index}",
                    _at(1),
                    completed,
                    0,
                    None,
                    None,
                    None,
                    None,
                    None,
                )
                for index, category, completed in (
                    (201, "completed", _at(3)),
                    (202, "canceled", _at(4)),
                    (203, "started", None),
                )
            ],
        )

        async with established.transaction():
            outcome = await apply_migration(
                established,
                MIGRATIONS_DIR / "031_analytics.sql",
                migrations_dir=MIGRATIONS_DIR,
            )

        assert "Applied migration 031" in outcome

        # And the index it built answers the query it was built for, over the
        # rows that were already there.
        repository = AnalyticsRepository()
        series = await repository.completion_series(
            established,
            scope=SCOPE_A,
            start=WINDOW_START,
            end=WINDOW_END,
            range_start=RANGE_START,
            days=WINDOW_DAYS,
            created={},
        )

        assert throughput_totals(series).completed == 1
        assert throughput_totals(series).canceled == 1
    finally:
        await established.close()


async def test_applying_031_a_second_time_changes_nothing(connection):
    """The ledger is what makes it a no-op, not `IF NOT EXISTS` in the file.

    A migration re-run has to be safe because a deploy can be retried, and the
    runner answers that from `schema_migrations` -- so the file's statements
    are never issued twice and a plain `CREATE INDEX` cannot fail on the
    second pass.
    """
    async with connection.transaction():
        outcome = await apply_migration(
            connection,
            MIGRATIONS_DIR / "031_analytics.sql",
            migrations_dir=MIGRATIONS_DIR,
        )

    assert "already applied" in outcome

    # Still exactly one index, and still exactly one ledger row.
    assert (
        await connection.fetchval(
            "SELECT count(*) FROM pg_indexes WHERE indexname = $1",
            "issues_workspace_completed_at_idx",
        )
        == 1
    )
    assert (
        await connection.fetchval(
            "SELECT count(*) FROM schema_migrations WHERE version = '031'"
        )
        == 1
    )
