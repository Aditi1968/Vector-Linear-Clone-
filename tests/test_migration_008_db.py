"""Migration 008's two guarantees, asked of a real PostgreSQL server.

`test_migration_lint.py` reads 008 as text and the service tests exercise the
translation layer above it. Neither can answer what this file asks, because
both of 008's rules are claims about what the *server* refuses, and a rule
that is only asserted in the language that states it is not a rule:

  * `cycles_no_overlap` is the reason this migration installs an extension.
    An EXCLUDE constraint and a service-level SELECT-then-INSERT are
    indistinguishable in every green test that inserts one row at a time, and
    differ entirely under two concurrent writers. The half-open range bound
    is the same kind of claim: `'[)'` and `'[]'` differ on exactly one input
    -- cycles written back to back -- which is the input teams produce most.
  * `issues_cycle_fk` is a composite foreign key over
    `(workspace_id, team_id, cycle_id)`. It is indistinguishable from a
    single-column `cycle_id` reference in any catalog summary that does not
    read the column order, and behaves identically until someone puts an
    issue into another team's cycle.

So the schema is built by the real runner over the real migration chain, and
everything afterwards is asked of the server rather than of the file.

Marked `db`: deselected by default, skipped when Docker is unreachable.
"""

from datetime import datetime, timedelta, timezone
from uuid import UUID

import asyncpg
import pytest

from tests.conftest import apply_all_migrations, reset_schema, seed_workflow_states


pytestmark = pytest.mark.db

# The tenant migrations/002_tenancy.sql seeds, and the team 005 gives a board
# to. Written as literals because the migrations write them as literals.
BOOTSTRAP_WORKSPACE_ID = UUID("00000000-0000-7000-8000-000000000001")
BOOTSTRAP_TEAM_ID = UUID("00000000-0000-7000-8000-000000000002")

# A second team in the SAME workspace. Same workspace is the point: it is what
# makes "another team's cycle" and "two teams in the same fortnight"
# expressible without a second tenant confounding the two.
SIBLING_TEAM_ID = UUID("00000000-0000-7000-8000-0000000000d1")

# A team with nothing else attached to it, for the one test whose subject is
# what refuses a team's deletion. Anything else referencing the team would
# refuse it first, and the test would pass without reaching 008's constraint.
SPARE_TEAM_ID = UUID("00000000-0000-7000-8000-0000000000d2")

JANUARY = datetime(2026, 1, 1, tzinfo=timezone.utc)
FORTNIGHT = timedelta(days=14)

INSERT_CYCLE_SQL = """
INSERT INTO cycles (workspace_id, team_id, number, name, starts_at, ends_at)
VALUES ($1, $2, $3, $4, $5, $6)
RETURNING id
"""

INSERT_ISSUE_SQL = """
INSERT INTO issues (
    workspace_id, team_id, number, workflow_state_id, title, priority, cycle_id
)
VALUES ($1, $2, $3, $4, $5, 1, $6)
RETURNING id
"""


@pytest.fixture
async def connection(postgres_dsn):
    """Every migration, in filename order, through the real runner.

    `apply_all_migrations` rather than a named prefix: 008 sits on top of the
    whole chain, and 006 and 007 belong to other branches. A hand-written list
    here would either go stale the day one of them lands or silently skip it.
    """
    connection = await asyncpg.connect(postgres_dsn)

    try:
        await reset_schema(connection)
        await apply_all_migrations(connection)
        yield connection
    finally:
        await connection.close()


async def test_008_applies_to_a_database_that_already_has_btree_gist(postgres_dsn):
    """The reason `CREATE EXTENSION` here is guarded.

    A managed Postgres may ship btree_gist pre-installed, and an operator
    evaluating it installs it by hand. Bare `CREATE EXTENSION` fails on such
    a database with `extension "btree_gist" already exists`, and because the
    runner never records a migration that errored, it fails there *every*
    time -- 008 could never be applied at all.

    So the extension is installed here before the chain runs, which is the
    one starting state the rest of this file's fixture deliberately clears.
    """
    connection = await asyncpg.connect(postgres_dsn)

    try:
        await reset_schema(connection)
        await connection.execute("CREATE EXTENSION btree_gist")

        # The assertion is that this does not raise.
        applied = await apply_all_migrations(connection)

        assert "008_cycles.sql" in applied
        assert await connection.fetchval("SELECT to_regclass('public.cycles')")
    finally:
        await connection.close()


@pytest.fixture
async def sibling_team(connection) -> asyncpg.Connection:
    """A second team in the bootstrap workspace, with a board of its own.

    005 seeds workflow states for the teams that exist when it runs, so a team
    created afterwards has none and cannot hold an issue at all.
    """
    await connection.execute(
        "INSERT INTO teams (id, workspace_id, name, key) VALUES ($1, $2, $3, $4)",
        SIBLING_TEAM_ID,
        BOOTSTRAP_WORKSPACE_ID,
        "Platform",
        "PLAT",
    )
    await seed_workflow_states(connection, BOOTSTRAP_WORKSPACE_ID, SIBLING_TEAM_ID)

    return connection


async def _add_cycle(
    connection,
    *,
    team_id: UUID = BOOTSTRAP_TEAM_ID,
    number: int,
    starts_at: datetime,
    ends_at: datetime,
    name: str | None = None,
) -> UUID:
    cycle_id: UUID = await connection.fetchval(
        INSERT_CYCLE_SQL,
        BOOTSTRAP_WORKSPACE_ID,
        team_id,
        number,
        name,
        starts_at,
        ends_at,
    )

    return cycle_id


async def _add_issue(
    connection,
    *,
    team_id: UUID = BOOTSTRAP_TEAM_ID,
    number: int,
    cycle_id: UUID | None = None,
) -> UUID:
    """One issue on a team, optionally already in a cycle.

    The workflow state is read back rather than named: 005 chooses the ids,
    and `workflow_state_id` is NOT NULL with no default.
    """
    state_id = await connection.fetchval(
        """
        SELECT id FROM workflow_states
        WHERE workspace_id = $1 AND team_id = $2 AND type = 'unstarted'
        ORDER BY position, id
        LIMIT 1
        """,
        BOOTSTRAP_WORKSPACE_ID,
        team_id,
    )

    issue_id: UUID = await connection.fetchval(
        INSERT_ISSUE_SQL,
        BOOTSTRAP_WORKSPACE_ID,
        team_id,
        number,
        state_id,
        f"issue {number}",
        cycle_id,
    )

    return issue_id


# --- cycles_no_overlap ------------------------------------------------


async def test_two_cycles_of_one_team_may_not_cover_the_same_instant(connection):
    """The constraint the extension is installed for.

    The second cycle starts one day before the first ends, which is the
    smallest overlap a fortnightly team can produce by hand.
    """
    await _add_cycle(
        connection, number=1, starts_at=JANUARY, ends_at=JANUARY + FORTNIGHT
    )

    with pytest.raises(asyncpg.ExclusionViolationError) as raised:
        await _add_cycle(
            connection,
            number=2,
            starts_at=JANUARY + FORTNIGHT - timedelta(days=1),
            ends_at=JANUARY + 2 * FORTNIGHT,
        )

    # The name is asserted because CycleService translates on exactly this
    # string; a renamed constraint would leave the service's `except` branch
    # unreachable and the client reading "Internal server error".
    assert raised.value.constraint_name == "cycles_no_overlap"


async def test_a_cycle_may_start_the_instant_the_previous_one_ends(connection):
    """The half-open bound, which is the ordinary case and not the edge case.

    Under '[]' these two would share the boundary instant and the constraint
    would refuse the most common thing a team does -- write its cycles back
    to back.
    """
    boundary = JANUARY + FORTNIGHT

    await _add_cycle(connection, number=1, starts_at=JANUARY, ends_at=boundary)
    await _add_cycle(
        connection, number=2, starts_at=boundary, ends_at=boundary + FORTNIGHT
    )

    assert await connection.fetchval("SELECT count(*) FROM cycles") == 2


async def test_two_teams_may_run_cycles_over_the_same_fortnight(sibling_team):
    """Why team_id is in the exclusion key at all.

    Without it the constraint would serialise the whole workspace onto one
    team's calendar, which is not a stricter rule but a wrong one.
    """
    await _add_cycle(
        sibling_team, number=1, starts_at=JANUARY, ends_at=JANUARY + FORTNIGHT
    )
    await _add_cycle(
        sibling_team,
        team_id=SIBLING_TEAM_ID,
        number=1,
        starts_at=JANUARY,
        ends_at=JANUARY + FORTNIGHT,
    )

    assert await sibling_team.fetchval("SELECT count(*) FROM cycles") == 2


async def test_a_cycle_that_ends_before_it_starts_is_refused(connection):
    with pytest.raises(asyncpg.CheckViolationError) as raised:
        await _add_cycle(
            connection,
            number=1,
            starts_at=JANUARY + FORTNIGHT,
            ends_at=JANUARY,
        )

    assert raised.value.constraint_name == "cycles_dates_ordered"


async def test_a_zero_length_cycle_is_refused(connection):
    """`>` and not `>=`: a cycle no issue can be worked in is not a cycle."""
    with pytest.raises(asyncpg.CheckViolationError):
        await _add_cycle(connection, number=1, starts_at=JANUARY, ends_at=JANUARY)


async def test_one_team_may_not_reuse_a_cycle_number(connection):
    await _add_cycle(
        connection, number=7, starts_at=JANUARY, ends_at=JANUARY + FORTNIGHT
    )

    with pytest.raises(asyncpg.UniqueViolationError) as raised:
        await _add_cycle(
            connection,
            number=7,
            starts_at=JANUARY + FORTNIGHT,
            ends_at=JANUARY + 2 * FORTNIGHT,
        )

    assert raised.value.constraint_name == "cycles_team_number_key"


async def test_two_teams_may_each_have_a_cycle_seven(sibling_team):
    """Numbering is per team, so `UNIQUE (number)` would be wrong."""
    await _add_cycle(
        sibling_team, number=7, starts_at=JANUARY, ends_at=JANUARY + FORTNIGHT
    )
    await _add_cycle(
        sibling_team,
        team_id=SIBLING_TEAM_ID,
        number=7,
        starts_at=JANUARY,
        ends_at=JANUARY + FORTNIGHT,
    )

    assert await sibling_team.fetchval("SELECT count(*) FROM cycles") == 2


# --- issues_cycle_fk --------------------------------------------------


async def test_an_issue_may_not_join_another_teams_cycle(sibling_team):
    """The composite foreign key, and the reason it is composite.

    A single-column `cycle_id -> cycles (id)` reference would accept this
    insert: the cycle exists, and nothing in a one-column key compares the
    issue's team to the cycle's.
    """
    other_teams_cycle = await _add_cycle(
        sibling_team,
        team_id=SIBLING_TEAM_ID,
        number=1,
        starts_at=JANUARY,
        ends_at=JANUARY + FORTNIGHT,
    )

    with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
        await _add_issue(sibling_team, number=1, cycle_id=other_teams_cycle)

    assert raised.value.constraint_name == "issues_cycle_fk"


async def test_an_issue_may_join_its_own_teams_cycle(connection):
    cycle_id = await _add_cycle(
        connection, number=1, starts_at=JANUARY, ends_at=JANUARY + FORTNIGHT
    )

    issue_id = await _add_issue(connection, number=1, cycle_id=cycle_id)

    stored = await connection.fetchval(
        "SELECT cycle_id FROM issues WHERE id = $1", issue_id
    )

    assert stored == cycle_id


async def test_an_issue_in_no_cycle_is_an_ordinary_issue(connection):
    """MATCH SIMPLE is the mechanism, not a footnote.

    A NULL among the referencing columns skips the check entirely, and
    cycle_id is the only one of the three that can be NULL. Under MATCH FULL
    every issue would have to be in a cycle.
    """
    issue_id = await _add_issue(connection, number=1)

    stored = await connection.fetchval(
        "SELECT cycle_id FROM issues WHERE id = $1", issue_id
    )

    assert stored is None


async def test_an_issue_may_not_move_to_a_team_its_cycle_does_not_belong_to(
    sibling_team,
):
    """The constraint holds in the other direction too.

    Moving an issue out of its team while it sits in that team's cycle would
    leave the stored triple matching no row in `cycles`.
    """
    cycle_id = await _add_cycle(
        sibling_team, number=1, starts_at=JANUARY, ends_at=JANUARY + FORTNIGHT
    )
    issue_id = await _add_issue(sibling_team, number=1, cycle_id=cycle_id)

    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await sibling_team.execute(
            "UPDATE issues SET team_id = $2 WHERE id = $1",
            issue_id,
            SIBLING_TEAM_ID,
        )


# --- ON DELETE SET NULL (cycle_id) ------------------------------------


async def test_deleting_a_cycle_disbands_it_and_destroys_no_work(connection):
    """The column list on SET NULL is what makes this statement possible.

    Bare `ON DELETE SET NULL` would null workspace_id and team_id as well,
    and the delete would fail on their NOT NULL constraints -- reporting a
    tenancy column to someone who asked to delete a cycle.
    """
    cycle_id = await _add_cycle(
        connection, number=1, starts_at=JANUARY, ends_at=JANUARY + FORTNIGHT
    )
    issue_id = await _add_issue(connection, number=1, cycle_id=cycle_id)

    await connection.execute("DELETE FROM cycles WHERE id = $1", cycle_id)

    row = await connection.fetchrow(
        "SELECT workspace_id, team_id, cycle_id FROM issues WHERE id = $1",
        issue_id,
    )

    assert row is not None, "deleting a cycle must not delete its issues"
    assert row["cycle_id"] is None
    assert row["workspace_id"] == BOOTSTRAP_WORKSPACE_ID
    assert row["team_id"] == BOOTSTRAP_TEAM_ID


async def test_a_team_with_cycles_may_not_be_deleted(connection):
    """RESTRICT, not CASCADE: 002's argument at issues_team_fk, unchanged.

    The team here is deliberately bare -- no workflow states, no issues -- so
    that `cycles_team_fk` is the only constraint standing in the delete's way.
    Given a team the fixtures had furnished, `workflow_states_team_fk` refuses
    the statement first and the assertion passes without 008 being involved
    at all.
    """
    await connection.execute(
        "INSERT INTO teams (id, workspace_id, name, key) VALUES ($1, $2, $3, $4)",
        SPARE_TEAM_ID,
        BOOTSTRAP_WORKSPACE_ID,
        "Spare",
        "SPARE",
    )
    await _add_cycle(
        connection,
        team_id=SPARE_TEAM_ID,
        number=1,
        starts_at=JANUARY,
        ends_at=JANUARY + FORTNIGHT,
    )

    # RestrictViolationError (23001), not ForeignKeyViolationError (23503).
    # They are siblings in asyncpg's hierarchy rather than parent and child:
    # RESTRICT is checked as the delete executes, where NO ACTION would defer
    # to the end of the statement and report the other code. Nothing in
    # app/services/ catches this one, and nothing should -- no service path
    # deletes a team.
    with pytest.raises(asyncpg.RestrictViolationError) as raised:
        await connection.execute("DELETE FROM teams WHERE id = $1", SPARE_TEAM_ID)

    assert raised.value.constraint_name == "cycles_team_fk"
