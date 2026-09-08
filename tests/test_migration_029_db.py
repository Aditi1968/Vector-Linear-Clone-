"""Migration 029 and the calendar, against a real PostgreSQL.

tests/test_recurrence.py holds the arithmetic and tests/test_schedule.py holds
the code around the two claims. What neither can hold is the claims themselves,
because both are properties of the SERVER: one is a primary key resolving a
conflict, the other is `FOR UPDATE SKIP LOCKED`. Those are what this file
exercises, and it does it by holding one transaction open while a second one
tries -- so the collision is forced rather than hoped for.

Six claims, in the order they would hurt:

  * A REMINDER IS NOT SENT TWICE. Not "usually" and not "the second pass
    notices": the second insert conflicts on a key the first has already
    written, and the second sweep is returned nothing for that issue.
  * TWO REPLICAS DO NOT BOTH REMIND. The same key, contended across two
    connections in the same instant.
  * A COMPLETED ISSUE IS NEVER REMINDED ABOUT. The statement joins
    `workflow_states` and requires a non-terminal category, so an issue
    finished before its date arrives is never selected at all.
  * A MOVED DUE DATE IS A NEW PROMISE, and one moved back onto a day already
    warned about is silent. Nothing is scheduled, so nothing is cancelled.
  * TWO REPLICAS DO NOT BOTH FILE A RECURRING ISSUE. One sweep locks, the other
    skips; the advance published by the first makes the row no longer due.
  * THE DUE FILTERS READ WHAT THEY CLAIM. `CURRENT_DATE` against a real server,
    over rows seeded relative to it.

Marked `db`: deselected by default, skipped when Docker is unreachable.
"""

from datetime import date, timedelta
from uuid import UUID

import asyncpg
import pytest

from app.domain.estimates import EstimateScale
from app.domain.issues import DEFAULT_ORDER, DueWindow, IssueFilter
from app.domain.recurrence import RecurrenceFrequency, RecurrenceRule
from app.domain.tenancy import WorkspaceScope
from app.repositories.issues import IssueRepository
from app.repositories.recurrences import RecurrenceRepository
from app.repositories.reminders import DueReminderRepository

from tests.conftest import apply_all_migrations, reset_schema, seed_workflow_states


pytestmark = pytest.mark.db

# The bootstrap tenant migrations/002_tenancy.sql seeds, reused rather than
# inserted: an issue has to hang off a team that has workflow states, and 002
# already built one.
WORKSPACE_A = UUID("00000000-0000-7000-8000-000000000001")
TEAM_A = UUID("00000000-0000-7000-8000-000000000002")

WORKSPACE_B = UUID("00000000-0000-7000-8000-0000000000a1")
TEAM_B = UUID("00000000-0000-7000-8000-0000000000a2")

SCOPE_A = WorkspaceScope(workspace_id=WORKSPACE_A)
SCOPE_B = WorkspaceScope(workspace_id=WORKSPACE_B)

TEMPLATE_ID = UUID("00000000-0000-7000-8000-0000000000c1")

# The lead time the sweep runs with here. One, matching
# `app.services.schedule.REMINDER_LEAD_DAYS`, so the window under test is the
# one the product uses rather than one this file made convenient.
LEAD_DAYS = 1

BATCH = 100

# (index, workspace, team, number, title, days from today, terminal)
#
# Seeded relative to the SERVER's today rather than to a fixed date, because
# `CURRENT_DATE` is what every predicate reads and a fixed date would make this
# file start failing on a particular morning.
ISSUE_SEED = (
    (1, WORKSPACE_A, TEAM_A, 1, "Ship the thing", 1, False),  # due tomorrow
    (2, WORKSPACE_A, TEAM_A, 2, "Due today", 0, False),
    (3, WORKSPACE_A, TEAM_A, 3, "Already late", -3, False),
    (4, WORKSPACE_A, TEAM_A, 4, "Next week", 8, False),
    (5, WORKSPACE_A, TEAM_A, 5, "Finished early", 1, True),
    (6, WORKSPACE_A, TEAM_A, 6, "No date at all", None, False),
    (7, WORKSPACE_B, TEAM_B, 1, "Another tenant's, due tomorrow", 1, False),
)

INSERT_ISSUE_SQL = """
INSERT INTO issues (
    id, workspace_id, team_id, number, workflow_state_id, title, due_date
)
VALUES (
    $1, $2, $3, $4,
    (
        SELECT id FROM workflow_states
        WHERE workspace_id = $2 AND team_id = $3 AND type = $6
        ORDER BY position
        LIMIT 1
    ),
    $5,
    CASE WHEN $7::int IS NULL THEN NULL ELSE CURRENT_DATE + $7::int END
)
"""

# Every reminder row whose ledger claims one workspace while its issue lives in
# another. The answer must be zero and there must be no way to make it
# otherwise: the composite foreign key is what enforces it, and this is the
# read that would notice if a sweep ever fanned out under a workspace it had
# not read off the row it claimed.
MISFILED_REMINDERS_SQL = """
SELECT count(*)
FROM issue_due_reminders AS reminder
JOIN issues ON issues.id = reminder.issue_id
WHERE issues.workspace_id <> reminder.workspace_id
"""


def issue_id(index: int) -> UUID:
    return UUID(f"a1b2c3d4-0000-4000-9000-{index:012d}")


async def _seed(connection) -> None:
    await reset_schema(connection)
    await apply_all_migrations(connection)

    await connection.execute(
        "INSERT INTO workspaces (id, slug, name) VALUES ($1, $2, $3)",
        WORKSPACE_B,
        "acme",
        "Acme",
    )
    await connection.execute(
        "INSERT INTO teams (id, workspace_id, name, key) VALUES ($1, $2, $3, $4)",
        TEAM_B,
        WORKSPACE_B,
        "Acme Core",
        "ACME",
    )
    await seed_workflow_states(connection, WORKSPACE_B, TEAM_B)

    for index, workspace, team, number, title, offset, terminal in ISSUE_SEED:
        await connection.execute(
            INSERT_ISSUE_SQL,
            issue_id(index),
            workspace,
            team,
            number,
            title,
            # The CATEGORY and never the name: 'Done' is a label a team may
            # rename, 'completed' is what migration 005's CHECK constrains.
            "completed" if terminal else "unstarted",
            offset,
        )

    await connection.execute(
        """
        INSERT INTO issue_templates (id, workspace_id, team_id, name, title)
        VALUES ($1, $2, $3, 'Weekly report', 'Weekly report')
        """,
        TEMPLATE_ID,
        WORKSPACE_A,
        TEAM_A,
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
async def second(postgres_dsn, connection):
    """A SECOND connection to the seeded database, for the contention tests.

    Depends on `connection` so the seed has already run; a fixture that
    connected first would race the schema rebuild.
    """
    established = await asyncpg.connect(postgres_dsn)

    try:
        yield established
    finally:
        await established.close()


@pytest.fixture
def reminders() -> DueReminderRepository:
    return DueReminderRepository()


@pytest.fixture
def recurrences() -> RecurrenceRepository:
    return RecurrenceRepository()


async def claim(connection, reminders, limit: int = BATCH):
    async with connection.transaction():
        return await reminders.claim_due(
            connection,
            lead_days=LEAD_DAYS,
            limit=limit,
        )


def weekly_rule(starts_on: date, **kwargs) -> RecurrenceRule:
    return RecurrenceRule(
        frequency=RecurrenceFrequency.WEEKLY,
        interval_count=1,
        starts_on=starts_on,
        weekdays=tuple(range(1, 8)),
        **kwargs,
    )


# --- who the reminder sweep selects -----------------------------------


async def test_the_sweep_claims_what_is_coming_due_and_nothing_else(
    connection, reminders
):
    """Four of the seven issues are excluded, each for its own reason.

    Overdue is excluded because a reminder is a WARNING and a warning about
    something that already happened is not one -- and because without the lower
    bound the first pass after this feature deploys fans out one notification
    per overdue issue in the installation.

    Next week is outside the lead time, the completed one is finished, and the
    undated one has promised nothing. The one in workspace B IS claimed: the
    sweep has no tenant, and the isolation property is that its workspace comes
    back on the row rather than that it never saw it.
    """
    claimed = await claim(connection, reminders)

    assert {(due.workspace_id, due.issue_id) for due in claimed} == {
        (WORKSPACE_A, issue_id(1)),
        (WORKSPACE_A, issue_id(2)),
        (WORKSPACE_B, issue_id(7)),
    }


async def test_a_completed_issue_is_never_claimed_even_on_its_due_date(
    connection, reminders
):
    """THE ASSERTION THE TASK SINGLES OUT: a reminder that fires for a closed
    issue is worse than no reminder.

    Issue 5 is due tomorrow and finished, and it is excluded by the join rather
    than by a filter applied afterwards -- so there is no delivery-time check to
    forget. Moving it back out of a terminal state makes it claimable again,
    which is what proves the join is reading the state and not something else
    about the row.
    """
    claimed = await claim(connection, reminders)

    assert issue_id(5) not in {due.issue_id for due in claimed}

    await connection.execute(
        """
        UPDATE issues
        SET workflow_state_id = (
            SELECT id FROM workflow_states
            WHERE workspace_id = $2 AND team_id = $3 AND type = 'started'
            ORDER BY position LIMIT 1
        )
        WHERE id = $1
        """,
        issue_id(5),
        WORKSPACE_A,
        TEAM_A,
    )

    assert issue_id(5) in {due.issue_id for due in await claim(connection, reminders)}


# --- exactly once -----------------------------------------------------


async def test_a_second_pass_claims_nothing_because_the_ledger_already_says_so(
    connection, reminders
):
    """The dedupe, over two sequential passes.

    This is the ordinary case rather than the contended one: the sweep runs
    every fifteen minutes and the second pass through it must be a no-op, or a
    person whose issue is due tomorrow receives ninety-six notifications about
    it.
    """
    first = await claim(connection, reminders)

    assert first

    assert await claim(connection, reminders) == []


async def test_two_replicas_claiming_at_the_same_instant_do_not_both_remind(
    connection, second, reminders
):
    """The exactly-once claim, FORCED rather than hoped for.

    One transaction claims and is held open, so its ledger rows are written and
    uncommitted; the second's INSERT then contends on the primary key. When the
    first commits, the second's `ON CONFLICT DO NOTHING` resolves against a row
    that now exists and it is returned nothing -- which is what makes each
    issue exactly one sweep's work.

    Written with an explicit BEGIN on the second connection so both are inside
    transactions at the same moment, which a sequential pair of `async with`
    blocks cannot arrange.
    """
    transaction = connection.transaction()
    await transaction.start()

    mine = await reminders.claim_due(connection, lead_days=LEAD_DAYS, limit=BATCH)

    assert mine

    # Started here rather than awaited to completion, because the INSERT will
    # block on the uncommitted key until the first transaction ends.
    other = second.transaction()
    await other.start()

    await transaction.commit()

    theirs = await reminders.claim_due(second, lead_days=LEAD_DAYS, limit=BATCH)
    await other.commit()

    assert theirs == []
    assert {due.issue_id for due in mine} == {
        issue_id(1),
        issue_id(2),
        issue_id(7),
    }


async def test_a_rolled_back_pass_leaves_the_issue_claimable(connection, reminders):
    """The other half of "the ledger row IS the claim".

    The fan-out runs inside the claiming transaction, so a notification that
    fails has to take the ledger row with it -- otherwise the issue is recorded
    as warned with nobody warned, and no later pass would ever select it again.
    """
    transaction = connection.transaction()
    await transaction.start()

    assert await reminders.claim_due(connection, lead_days=LEAD_DAYS, limit=BATCH)

    await transaction.rollback()

    assert await claim(connection, reminders)


# --- a moved due date -------------------------------------------------


async def test_moving_a_due_date_forward_is_a_new_promise_and_is_warned_about(
    connection, reminders
):
    """Nothing was scheduled, so nothing had to be cancelled.

    The sweep reads each issue's CURRENT due date on every pass and the key
    names that date, so a moved commitment is simply a pair the ledger has not
    seen. Every scheduled-job version of this feature needs a cancel path to
    get here.
    """
    await claim(connection, reminders)

    await connection.execute(
        "UPDATE issues SET due_date = CURRENT_DATE WHERE id = $1",
        issue_id(1),
    )

    assert issue_id(1) in {due.issue_id for due in await claim(connection, reminders)}


async def test_moving_a_due_date_back_onto_a_warned_day_stays_silent(
    connection, reminders
):
    """That person was already told, and the primary key is what remembers it.

    Issue 2 is warned about today; moving it to tomorrow warns again (a new
    promise), and moving it back to today collides with the row still in the
    ledger.
    """
    await claim(connection, reminders)

    await connection.execute(
        "UPDATE issues SET due_date = CURRENT_DATE + 1 WHERE id = $1",
        issue_id(2),
    )

    assert issue_id(2) in {due.issue_id for due in await claim(connection, reminders)}

    await connection.execute(
        "UPDATE issues SET due_date = CURRENT_DATE WHERE id = $1",
        issue_id(2),
    )

    assert await claim(connection, reminders) == []


async def test_clearing_a_due_date_stops_the_reminder_with_nothing_to_cancel(
    connection, reminders
):
    await connection.execute(
        "UPDATE issues SET due_date = NULL WHERE id = $1",
        issue_id(1),
    )

    assert issue_id(1) not in {
        due.issue_id for due in await claim(connection, reminders)
    }


async def test_no_reminder_row_can_name_another_tenants_issue(connection, reminders):
    """The composite foreign key, asserted over the rows a real sweep wrote.

    A count rather than an attempted insert, because a count is the read a
    cross-tenant defect would hide in: a sweep that built its scope from
    anything but the claimed row would leave rows this query finds.
    """
    await claim(connection, reminders)

    assert await connection.fetchval(MISFILED_REMINDERS_SQL) == 0

    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await connection.execute(
            """
            INSERT INTO issue_due_reminders (workspace_id, issue_id, due_date)
            VALUES ($1, $2, CURRENT_DATE)
            """,
            WORKSPACE_B,
            issue_id(1),
        )


# --- recurrences ------------------------------------------------------


async def test_two_replicas_claiming_one_schedule_do_not_both_file(
    connection, second, recurrences
):
    """`FOR UPDATE SKIP LOCKED` plus the advance, forced across two connections.

    The first transaction locks the due row and moves `next_run_on` past today;
    the second, running while that lock is held, SKIPS it rather than blocking
    -- which is what keeps two replicas from waiting on each other. After the
    commit it is not due at all, so a third pass finds nothing either.
    """
    today = await connection.fetchval("SELECT CURRENT_DATE")

    async with connection.transaction():
        await recurrences.upsert(
            connection,
            scope=SCOPE_A,
            template_id=TEMPLATE_ID,
            team_id=TEAM_A,
            rule=weekly_rule(today),
            next_run_on=today,
        )

    transaction = connection.transaction()
    await transaction.start()

    claimed_today, mine = await recurrences.lock_due(connection, limit=BATCH)

    assert [entity.template_id for _, entity in mine] == [TEMPLATE_ID]
    assert claimed_today == today

    # Held open, so the row is locked. The second sweep skips it rather than
    # waiting, which is the property that would otherwise be a deadlock or a
    # double file.
    _, theirs = await recurrences.lock_due(second, limit=BATCH)

    assert theirs == []

    await recurrences.advance(
        connection,
        workspace_id=WORKSPACE_A,
        template_id=TEMPLATE_ID,
        next_run_on=today + timedelta(days=1),
    )
    await transaction.commit()

    _, after = await recurrences.lock_due(second, limit=BATCH)

    assert after == []


async def test_a_schedule_reads_back_as_it_was_written(connection, recurrences):
    """Every column round-trips, including the array and the two nullables.

    A monthly rule, so `day_of_month` is exercised and `weekdays` is NULL --
    which the mapper turns into `()` rather than None, because
    `RecurrenceRule.weekdays` promises `in` works without a guard.
    """
    today = await connection.fetchval("SELECT CURRENT_DATE")
    rule = RecurrenceRule(
        frequency=RecurrenceFrequency.MONTHLY,
        interval_count=3,
        starts_on=today,
        day_of_month=31,
        due_in_days=5,
    )

    async with connection.transaction():
        written = await recurrences.upsert(
            connection,
            scope=SCOPE_A,
            template_id=TEMPLATE_ID,
            team_id=TEAM_A,
            rule=rule,
            next_run_on=today,
        )

    assert written is not None
    assert written.rule == rule
    assert written.weekdays == ()
    assert written.team_id == TEAM_A

    found = await recurrences.find_many_for_templates(
        connection,
        scope=SCOPE_A,
        template_ids=[TEMPLATE_ID],
    )

    assert found[TEMPLATE_ID].rule == rule


async def test_a_schedule_is_invisible_to_another_workspace(connection, recurrences):
    """The tenant predicate on the client-facing reads, which the sweep's own
    statement deliberately does not carry -- see the repository."""
    today = await connection.fetchval("SELECT CURRENT_DATE")

    async with connection.transaction():
        await recurrences.upsert(
            connection,
            scope=SCOPE_A,
            template_id=TEMPLATE_ID,
            team_id=TEAM_A,
            rule=weekly_rule(today),
            next_run_on=today,
        )

    assert (
        await recurrences.find_many_for_templates(
            connection,
            scope=SCOPE_B,
            template_ids=[TEMPLATE_ID],
        )
        == {}
    )

    async with connection.transaction():
        assert not await recurrences.delete(
            connection,
            scope=SCOPE_B,
            template_id=TEMPLATE_ID,
        )


async def test_a_schedule_cannot_name_another_workspaces_team(connection, recurrences):
    """One `workspace_id` column feeds both composite keys, so a schedule
    pairing A's template with B's team is not a row that exists to be claimed --
    which is what makes the sweep's tenantless read trustworthy."""
    today = await connection.fetchval("SELECT CURRENT_DATE")

    with pytest.raises(asyncpg.ForeignKeyViolationError):
        async with connection.transaction():
            await recurrences.upsert(
                connection,
                scope=SCOPE_A,
                template_id=TEMPLATE_ID,
                team_id=TEAM_B,
                rule=weekly_rule(today),
                next_run_on=today,
            )


@pytest.mark.parametrize(
    ("frequency", "weekdays", "day_of_month"),
    [
        # Weekly with no weekdays: a schedule that never fires.
        (RecurrenceFrequency.WEEKLY, (), None),
        # Daily carrying weekdays: a row two readers would disagree about.
        (RecurrenceFrequency.DAILY, (1,), None),
        # Monthly with no day; and a weekly one carrying both.
        (RecurrenceFrequency.MONTHLY, (), None),
        (RecurrenceFrequency.WEEKLY, (1,), 5),
    ],
)
async def test_the_schema_refuses_a_rule_whose_halves_do_not_match(
    connection, recurrences, frequency, weekdays, day_of_month
):
    """The CHECKs behind `RecurrenceRule.is_consistent`.

    Two copies of one rule, and both are wanted: the application's is what
    tells a client which field is wrong, and the constraint is what refuses
    every write that does not come through it.
    """
    today = await connection.fetchval("SELECT CURRENT_DATE")

    with pytest.raises(asyncpg.CheckViolationError):
        async with connection.transaction():
            await recurrences.upsert(
                connection,
                scope=SCOPE_A,
                template_id=TEMPLATE_ID,
                team_id=TEAM_A,
                rule=RecurrenceRule(
                    frequency=frequency,
                    interval_count=1,
                    starts_on=today,
                    weekdays=weekdays,
                    day_of_month=day_of_month,
                ),
                next_run_on=today,
            )


@pytest.mark.parametrize("weekday", [0, 8])
async def test_the_schema_refuses_a_weekday_outside_the_iso_range(
    connection, recurrences, weekday
):
    """ISO weekdays start at 1, so 0 is not Sunday here and 8 is nothing."""
    today = await connection.fetchval("SELECT CURRENT_DATE")

    with pytest.raises(asyncpg.CheckViolationError):
        async with connection.transaction():
            await recurrences.upsert(
                connection,
                scope=SCOPE_A,
                template_id=TEMPLATE_ID,
                team_id=TEAM_A,
                rule=RecurrenceRule(
                    frequency=RecurrenceFrequency.WEEKLY,
                    interval_count=1,
                    starts_on=today,
                    weekdays=(weekday,),
                ),
                next_run_on=today,
            )


async def test_a_template_cannot_be_deleted_while_its_schedule_stands(
    connection, recurrences
):
    """RESTRICT, so a one-line delete cannot silently stop a schedule.

    `TemplateService.delete` removes the recurrence itself, in the same
    transaction, which is the constraint working as a guard on that ordering
    rather than an obstacle to it -- the same shape 020 gives the label rows.
    """
    today = await connection.fetchval("SELECT CURRENT_DATE")

    async with connection.transaction():
        await recurrences.upsert(
            connection,
            scope=SCOPE_A,
            template_id=TEMPLATE_ID,
            team_id=TEAM_A,
            rule=weekly_rule(today),
            next_run_on=today,
        )

    # `RestrictViolationError` and not `ForeignKeyViolationError`: asyncpg
    # gives a RESTRICT refused on the REFERENCED side its own class, and the
    # two are siblings under `IntegrityConstraintViolationError` rather than
    # one deriving from the other. Naming the right one is the difference
    # between this test asserting the constraint and asserting that something
    # raised.
    with pytest.raises(asyncpg.RestrictViolationError):
        async with connection.transaction():
            await connection.execute(
                "DELETE FROM issue_templates WHERE id = $1",
                TEMPLATE_ID,
            )


# --- the due filters, against a real CURRENT_DATE ---------------------


@pytest.mark.parametrize(
    ("window", "expected"),
    [
        (DueWindow.OVERDUE, {3}),
        (DueWindow.TODAY, {2}),
        # Today and the six days after it: 1 is tomorrow and 4 is eight days
        # out, so the second is outside a rolling week.
        (DueWindow.THIS_WEEK, {1, 2, 5}),
        (DueWindow.NO_DUE_DATE, {6}),
    ],
)
async def test_each_window_selects_what_it_names(connection, window, expected):
    """Run against the server's own `CURRENT_DATE`, over rows seeded relative
    to it -- so this asserts the predicate rather than a date somebody wrote
    down.

    Scoped to workspace A, which is also the isolation assertion: B's issue is
    due tomorrow and appears in none of these.
    """
    rows = await IssueRepository().list(
        connection,
        scope=SCOPE_A,
        issue_filter=IssueFilter(due_window=window),
        order=DEFAULT_ORDER,
        limit=BATCH,
        after=None,
    )

    assert {row.number for row in rows} == expected


async def test_a_range_selects_both_of_its_ends(connection):
    today = await connection.fetchval("SELECT CURRENT_DATE")

    rows = await IssueRepository().list(
        connection,
        scope=SCOPE_A,
        issue_filter=IssueFilter(
            due_after=today - timedelta(days=3),
            due_before=today + timedelta(days=1),
        ),
        order=DEFAULT_ORDER,
        limit=BATCH,
        after=None,
    )

    assert {row.number for row in rows} == {1, 2, 3, 5}


async def test_the_due_ordering_walks_the_partial_due_date_index(connection):
    """Migration 029 adds no index, and this is the assertion behind that.

    `issues_workspace_live_due_date_id_idx` from 015 is `(workspace_id,
    due_date, id) WHERE archived_at IS NULL`, and every window this feature
    emits is a bound on `due_date` under that same tenant equality and the same
    partial predicate -- so the index the ordering already needed is the index
    the filters need.

    Asserted through `ORDER BY due_date`, which is the shape that forces the
    choice: a filtered read of a six-row table can be served by any index once
    `enable_seqscan` is off, so a plan assertion on the predicate alone would
    pass whichever index the planner happened to pick. Ordering is what only
    this index can supply without a sort, and `Sort` appearing in the plan is
    the failure -- which is what a predicate written as `age(due_date)` or a
    widened index would produce.
    """
    await connection.execute("SET enable_seqscan = off")

    plan = await connection.fetchval(
        """
        EXPLAIN (FORMAT TEXT)
        SELECT id FROM issues
        WHERE workspace_id = $1
            AND archived_at IS NULL
            AND due_date < CURRENT_DATE
        ORDER BY due_date, id
        """,
        WORKSPACE_A,
    )

    assert "issues_workspace_live_due_date_id_idx" in plan
    assert "Sort" not in plan


async def test_the_index_the_windows_rely_on_still_has_the_shape_they_need(
    connection,
):
    """015's definition, read out of the catalog rather than out of its file.

    The file says what was written; this says what the server holds, which is
    what a predicate is actually matched against. Both halves matter: the
    partial `WHERE archived_at IS NULL` is what every window carries alongside
    the tenant equality, and `due_date` leading the non-tenant part is what
    makes a range a bound rather than a filter.
    """
    definition = await connection.fetchval(
        """
        SELECT indexdef FROM pg_indexes
        WHERE tablename = 'issues'
            AND indexname = 'issues_workspace_live_due_date_id_idx'
        """
    )

    assert definition is not None
    assert "(workspace_id, due_date, id)" in definition
    assert "WHERE (archived_at IS NULL)" in definition


# --- the estimate scale -----------------------------------------------


async def test_every_team_starts_on_the_scale_its_existing_estimates_meant(
    connection,
):
    """The default, read back off a team migration 002 seeded before 029 ran.

    That is the backfill assertion: this team predates the column, so if the
    default were anything but `none` its issues' estimates would have changed
    meaning without a single row being written.
    """
    scale = await connection.fetchval(
        "SELECT estimate_scale FROM teams WHERE id = $1",
        TEAM_A,
    )

    assert EstimateScale(scale) is EstimateScale.NONE


async def test_the_scale_read_for_an_issue_is_its_own_teams(connection):
    await connection.execute(
        "UPDATE teams SET estimate_scale = 'tshirt' WHERE id = $1",
        TEAM_A,
    )

    assert (
        await IssueRepository().find_estimate_scale(
            connection,
            scope=SCOPE_A,
            issue_id=issue_id(1),
        )
        is EstimateScale.TSHIRT
    )

    # Another tenant's issue answers None, which is the same answer an id that
    # exists nowhere gives -- the property every read on that class has.
    assert (
        await IssueRepository().find_estimate_scale(
            connection,
            scope=SCOPE_A,
            issue_id=issue_id(7),
        )
        is None
    )


async def test_the_schema_refuses_a_scale_it_does_not_know(connection):
    with pytest.raises(asyncpg.CheckViolationError):
        await connection.execute(
            "UPDATE teams SET estimate_scale = 'fibonacci' WHERE id = $1",
            TEAM_A,
        )
