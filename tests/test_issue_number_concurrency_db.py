"""Issue number allocation under real concurrency, against real PostgreSQL.

This is the load-bearing test of migration 005. Everything else about team
keys and workflow states is a schema claim that a catalog query can settle;
this one is a claim about what happens when two people file an issue against
the same team at the same instant, and nothing but a concurrent run against a
real server can settle it.

## The guarantee under test

`TeamService.allocate_issue_number` promises, per team:

  * **unique** -- no two allocations ever return the same number, at any
    concurrency;
  * **contiguous** -- the numbers that end up committed on a team are 1..n
    with no holes, because `issue_counter` is an ordinary transactional
    column: a creation that fails takes its increment down with it and its
    number is handed to the next caller instead of being burnt. (A per-team
    sequence would fail exactly here, since `nextval()` is exempt from
    rollback by design.)
  * **independent per team** -- one team's numbering says nothing about
    another's, including another in the same workspace.

The guarantee is conditional on how the allocator is used, and the condition
is part of the contract rather than a caveat on it: the allocating transaction
must insert exactly one issue carrying the number, and must fail as a whole if
that insert fails. Allocate and commit without using the number and the hole
is permanent, whatever this test says.

Sections A, B and C assert those properties, in that order.

## Why a control is included

Section D runs the forbidden allocator -- `SELECT max(number) + 1` -- through
the *same* harness, and asserts that it fails. Without it, section A proves
nothing: a harness whose tasks happen to run one after another reports success
for any allocator at all, correct or not. The control is what establishes that
these tasks really do overlap, so that section A's silence is evidence.

Marked `db`: deselected by default, skipped when Docker is unreachable. Nothing
here touches DATABASE_URL or Neon; the only server it speaks to is the
throwaway container `postgres_dsn` starts.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import asyncpg
import pytest

from app.domain.errors import TeamNotFoundError
from app.domain.tenancy import WorkspaceScope
from app.repositories.teams import TeamRepository
from app.services.teams import TeamService
from scripts.apply_migration import apply_migration, read_migration

from tests.conftest import reset_schema


pytestmark = pytest.mark.db

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"
MIGRATION_001 = MIGRATIONS_DIR / "001_issues.sql"
MIGRATION_002 = MIGRATIONS_DIR / "002_tenancy.sql"
MIGRATION_005 = MIGRATIONS_DIR / "005_team_workflows.sql"

# 002's bootstrap tenant, written as literals for the reason 002 names them:
# so a test asserts against a constant instead of querying for the value it is
# about to check.
WORKSPACE_ID = UUID("00000000-0000-7000-8000-000000000001")
TEAM_ID = UUID("00000000-0000-7000-8000-000000000002")

# A second team in the SAME workspace. Same-workspace is the point: two teams
# in different workspaces could share a counter and still look independent,
# because nothing else about them would collide.
SECOND_TEAM_ID = UUID("00000000-0000-7000-8000-0000000000e1")
SECOND_TEAM_KEY = "DES"

# A team id that resolves to nothing, and the same team id in a workspace that
# is not its own. Both must be refused identically.
MISSING_TEAM_ID = UUID("00000000-0000-7000-8000-0000000000f1")
FOREIGN_WORKSPACE_ID = UUID("00000000-0000-7000-8000-0000000000f2")
FOREIGN_TEAM_ID = UUID("00000000-0000-7000-8000-0000000000f3")

SCOPE = WorkspaceScope(workspace_id=WORKSPACE_ID)

# How many creations run at once.
#
# Large enough that a serial harness would be obvious in the timings, small
# enough to sit well inside PostgreSQL's default max_connections of 100 with
# the session's other pools open. Every task holds its own connection for the
# whole of its transaction, so this is also the pool size below.
CONCURRENCY = 32

# Held between allocating a number and inserting the issue that uses it.
#
# This is the whole reason the test can see a race. Without it each task's
# transaction is short enough that the event loop may well run them one after
# another, and a serial run agrees with every allocator ever written. With it,
# every task has allocated before any task commits -- which is precisely the
# window `SELECT max(number) + 1` reads its answer from.
LOCK_HOLD_SECONDS = 0.02

INSERT_ISSUE_SQL = """
INSERT INTO issues (
    workspace_id,
    team_id,
    workflow_state_id,
    number,
    title,
    priority
)
VALUES ($1, $2, $3, $4, $5, 0)
RETURNING number
"""

# The forbidden allocator, spelled out so section D runs the real thing rather
# than a caricature of it. No lock is taken by this SELECT, which is the entire
# problem: two transactions read the same maximum and compute the same
# successor, and the schema is what refuses the second one.
NAIVE_ALLOCATE_SQL = """
SELECT coalesce(max(number), 0) + 1
FROM issues
WHERE team_id = $1
"""

SEED_ISSUE_SQL = """
INSERT INTO issues (title, description, priority, completed_at, created_at)
VALUES ($1, $2, $3, $4, $5)
"""

# Two rows that predate 002 and 005 entirely, so the counter starts at a
# non-zero high-water mark rather than at 0. A test that begins from an empty
# table cannot tell "the counter was moved past the backfilled numbers" from
# "the counter happens to start where the numbering does".
SEED_CREATED_AT = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
SEED_ISSUES = (
    ("oldest issue", None, 1, None),
    ("completed issue", "done already", 2, SEED_CREATED_AT),
)
SEED_COUNT = len(SEED_ISSUES)


@dataclass(frozen=True, slots=True)
class Fixture:
    pool: asyncpg.Pool
    connection: asyncpg.Connection
    service: TeamService
    state_id: UUID
    second_state_id: UUID


async def _rebuild(connection: asyncpg.Connection) -> None:
    """001's schema, populated, then 002 and 005 through the real runner.

    The seed rows go in between 001 and 002 on purpose: they are rows that
    existed before either migration, which is the only genealogy in which the
    backfills mean anything.
    """
    await reset_schema(connection)
    await connection.execute(read_migration(MIGRATION_001))
    await connection.executemany(
        SEED_ISSUE_SQL,
        [
            (title, description, priority, completed_at, SEED_CREATED_AT)
            for title, description, priority, completed_at in SEED_ISSUES
        ],
    )

    for migration in (MIGRATION_002, MIGRATION_005):
        async with connection.transaction():
            await apply_migration(connection, migration, migrations_dir=MIGRATIONS_DIR)


async def _state_id(connection: asyncpg.Connection, team_id: UUID) -> UUID:
    """The team's 'unstarted' state, looked up by category and never by name.

    By category because that is the discipline 005 asks of the application:
    'Todo' is a default the team may rename, 'unstarted' is not.
    """
    state_id = await connection.fetchval(
        """
        SELECT id
        FROM workflow_states
        WHERE workspace_id = $1 AND team_id = $2 AND type = 'unstarted'
        """,
        WORKSPACE_ID,
        team_id,
    )

    assert state_id is not None, "005 seeded no unstarted state for this team"

    return state_id


@pytest.fixture
async def fixture(postgres_dsn):
    """A migrated database, a second team, and a pool wide enough to race on.

    `max_size` is CONCURRENCY, not a smaller number: every task holds its
    connection for the length of its transaction, so a narrower pool would
    make the tasks queue on connection acquisition and turn the whole test
    into a serial run that passes for the wrong reason.
    """
    connection = await asyncpg.connect(postgres_dsn)
    pool = None

    try:
        await _rebuild(connection)

        await connection.execute(
            "INSERT INTO teams (id, workspace_id, name, key) VALUES ($1, $2, $3, $4)",
            SECOND_TEAM_ID,
            WORKSPACE_ID,
            "Design",
            SECOND_TEAM_KEY,
        )
        # A team created after 005 gets no workflow states from the migration,
        # so the test supplies the one it needs. That the seed is a migration
        # concern and team creation is an application concern is a real gap,
        # and it is recorded in the report rather than papered over here.
        await connection.execute(
            """
            INSERT INTO workflow_states
                (workspace_id, team_id, name, type, position)
            VALUES ($1, $2, 'Todo', 'unstarted', 0)
            """,
            WORKSPACE_ID,
            SECOND_TEAM_ID,
        )

        pool = await asyncpg.create_pool(
            dsn=postgres_dsn,
            min_size=CONCURRENCY,
            max_size=CONCURRENCY,
        )

        yield Fixture(
            pool=pool,
            connection=connection,
            service=TeamService(pool=pool, repository=TeamRepository()),
            state_id=await _state_id(connection, TEAM_ID),
            second_state_id=await _state_id(connection, SECOND_TEAM_ID),
        )
    finally:
        if pool is not None:
            await pool.close()

        await connection.close()


async def _create_issue(
    fixture: Fixture,
    *,
    team_id: UUID,
    state_id: UUID,
    index: int,
    rollback: bool = False,
) -> int:
    """One issue creation, shaped exactly as the real path will be.

    The service opens nothing: this function owns the transaction, allocates
    inside it, and inserts inside it. That is the contract
    `allocate_issue_number` documents, and the reason it takes a connection.

    `rollback=True` raises after allocating, which is how section C aborts a
    creation at exactly the point that matters without waiting for a real
    failure to occur by luck.
    """
    async with fixture.pool.acquire() as connection:
        async with connection.transaction():
            number = await fixture.service.allocate_issue_number(
                connection,
                scope=SCOPE,
                team_id=team_id,
            )

            # Every task allocates before any task commits.
            await asyncio.sleep(LOCK_HOLD_SECONDS)

            if rollback:
                raise _Rollback(number)

            await connection.execute(
                INSERT_ISSUE_SQL,
                WORKSPACE_ID,
                team_id,
                state_id,
                number,
                f"concurrent issue {index}",
            )

    return number


class _Rollback(Exception):
    """Aborts a creation after its number is allocated, carrying the number.

    A custom type rather than a bare RuntimeError so that
    `gather(return_exceptions=True)` can tell a deliberate abort apart from a
    real failure. Without that distinction a genuine defect -- a deadlock, a
    constraint violation, a bug in the service -- would be counted as one of
    the aborts the test arranged and the run would pass.
    """

    def __init__(self, number: int):
        super().__init__("deliberate rollback")

        self.number = number


async def _numbers(connection: asyncpg.Connection, team_id: UUID) -> list[int]:
    rows = await connection.fetch(
        "SELECT number FROM issues WHERE team_id = $1 ORDER BY number",
        team_id,
    )

    return [row["number"] for row in rows]


async def _counter(connection: asyncpg.Connection, team_id: UUID) -> int:
    counter = await connection.fetchval(
        "SELECT issue_counter FROM teams WHERE id = $1",
        team_id,
    )

    return int(counter)


# --------------------------------------------------------------------------
# The starting state the rest of the file reasons from
# --------------------------------------------------------------------------


async def test_the_backfill_numbered_existing_issues_and_moved_the_counter_past_them(
    fixture,
):
    """005's two backfills, checked together because they are one claim.

    Numbering the existing rows and leaving the counter at 0 would be worse
    than doing neither: the first issue created afterwards would allocate 1 and
    collide with the oldest issue on the team. The counter has to end up at the
    high-water mark, and this is the assertion that says so before any of the
    concurrency tests below rely on it.
    """
    assert await _numbers(fixture.connection, TEAM_ID) == [1, 2]
    assert await _counter(fixture.connection, TEAM_ID) == SEED_COUNT

    # The second team was created after 005 and has never had an issue, so its
    # counter is the column default rather than anything the backfill wrote.
    assert await _counter(fixture.connection, SECOND_TEAM_ID) == 0


# --------------------------------------------------------------------------
# A. Unique, and gapless when everything commits
# --------------------------------------------------------------------------


async def test_concurrent_creations_receive_unique_gapless_numbers(fixture):
    """CONCURRENCY simultaneous creations against one team.

    Four assertions, and each one fails for a different defect:

      * every allocation returned a distinct number -- the property a
        read-then-write allocator loses;
      * the returned numbers are exactly the contiguous run following the
        high-water mark -- so nothing was skipped and nothing restarted;
      * the rows in the table carry those same numbers -- so the numbers were
        not merely returned but actually used, and no insert was lost;
      * the counter ends at the last number handed out -- so the next creation
        continues the run rather than repeating part of it.

    Gaplessness is asserted here because every transaction in this test
    commits. Section C is the same test with failures in it, and it
    deliberately does not assert gaplessness.
    """
    allocated = await asyncio.gather(
        *[
            _create_issue(
                fixture,
                team_id=TEAM_ID,
                state_id=fixture.state_id,
                index=index,
            )
            for index in range(CONCURRENCY)
        ]
    )

    expected = list(range(SEED_COUNT + 1, SEED_COUNT + 1 + CONCURRENCY))

    assert len(set(allocated)) == CONCURRENCY, (
        f"{CONCURRENCY - len(set(allocated))} allocations collided: {allocated}"
    )
    assert sorted(allocated) == expected
    assert await _numbers(fixture.connection, TEAM_ID) == (
        list(range(1, SEED_COUNT + 1)) + expected
    )
    assert await _counter(fixture.connection, TEAM_ID) == SEED_COUNT + CONCURRENCY


async def test_the_unique_constraint_refuses_a_number_that_was_already_used(fixture):
    """The database's own guard, independent of the allocator.

    The allocator is what makes duplicates never happen; this constraint is
    what makes them impossible. Both are needed: a future caller that bypasses
    `allocate_issue_number` -- a script, a backfill, an import -- must not be
    able to write two ENG-1s, and no amount of care in the service can prevent
    that from outside the service.
    """
    with pytest.raises(asyncpg.UniqueViolationError) as error:
        await fixture.connection.execute(
            INSERT_ISSUE_SQL,
            WORKSPACE_ID,
            TEAM_ID,
            fixture.state_id,
            1,
            "duplicate number",
        )

    assert "issues_team_number_key" in str(error.value)


# --------------------------------------------------------------------------
# B. Independent sequences per team
# --------------------------------------------------------------------------


async def test_two_teams_in_one_workspace_have_independent_sequences(fixture):
    """DES-1 exists even though ENG-1 already does.

    Both teams are in the SAME workspace, and the two runs are interleaved
    rather than sequential, so a counter that was really per workspace -- or a
    lock taken on the workspace rather than the team -- would show up as one
    shared run split across the two teams instead of two runs starting at 1.
    """
    per_team = 8

    allocated = await asyncio.gather(
        *[
            _create_issue(
                fixture,
                team_id=team_id,
                state_id=state_id,
                index=index,
            )
            for index in range(per_team)
            for team_id, state_id in (
                (TEAM_ID, fixture.state_id),
                (SECOND_TEAM_ID, fixture.second_state_id),
            )
        ]
    )

    # The gather above alternates teams, so the two halves are interleaved in
    # `allocated`; what matters is what each team ended up holding.
    assert len(allocated) == per_team * 2

    assert await _numbers(fixture.connection, TEAM_ID) == list(
        range(1, SEED_COUNT + per_team + 1)
    )
    assert await _numbers(fixture.connection, SECOND_TEAM_ID) == list(
        range(1, per_team + 1)
    )

    assert await _counter(fixture.connection, TEAM_ID) == SEED_COUNT + per_team
    assert await _counter(fixture.connection, SECOND_TEAM_ID) == per_team


# --------------------------------------------------------------------------
# C. What a rollback does to the numbering
# --------------------------------------------------------------------------


async def test_a_rolled_back_creation_returns_its_number_instead_of_burning_it(
    fixture,
):
    """The property a per-team sequence could not provide.

    Half the creations abort after allocating. `issue_counter` is an ordinary
    column, so each abort takes its own increment down with it and the number
    goes back into circulation -- which is why the numbers that survive are
    still 1..n with no holes, even though a third of the work in this test
    failed.

    The first version of this test asserted the opposite, on the assumption
    that a rolled-back allocation burns its number the way `nextval()` does.
    It does not, and the difference is the entire reason 005 uses a counter
    column rather than a sequence. What is asserted instead:

      * the committed numbers are unique and contiguous, continuing the run
        the backfill left off at;
      * the stored rows are exactly those numbers -- the aborted transactions
        inserted nothing;
      * the counter ends at the number of *committed* allocations, not at the
        number of attempts.

    Not asserted: that a specific rolled-back number was reissued. It usually
    is, but only when a rollback happens to precede a later allocation, and the
    interleaving is the server's to choose.
    """
    attempts = CONCURRENCY
    results = await asyncio.gather(
        *[
            _create_issue(
                fixture,
                team_id=TEAM_ID,
                state_id=fixture.state_id,
                index=index,
                rollback=index % 2 == 0,
            )
            for index in range(attempts)
        ],
        return_exceptions=True,
    )

    committed = [result for result in results if isinstance(result, int)]
    rolled_back = [result for result in results if isinstance(result, _Rollback)]

    assert len(committed) == attempts // 2
    assert len(rolled_back) == attempts // 2

    expected = list(range(SEED_COUNT + 1, SEED_COUNT + 1 + len(committed)))

    assert sorted(committed) == expected, (
        "the surviving numbers are not a contiguous run: a rolled-back "
        f"allocation left a hole. Got {sorted(committed)}"
    )

    stored = await _numbers(fixture.connection, TEAM_ID)

    assert stored == list(range(1, SEED_COUNT + 1)) + expected

    # The counter counts commits, not attempts: half of these transactions
    # took their increment down with them.
    assert await _counter(fixture.connection, TEAM_ID) == SEED_COUNT + len(committed)

    # And the next creation continues the run rather than repeating any of it.
    following = await _create_issue(
        fixture,
        team_id=TEAM_ID,
        state_id=fixture.state_id,
        index=attempts,
    )

    assert following == max(committed) + 1


async def test_a_number_a_failed_transaction_allocated_is_handed_to_the_next_caller(
    fixture,
):
    """The same property again, sequentially, so it is stated rather than
    inferred from a concurrent run.

    Two allocations, ordered by construction: the first aborts, the second
    follows it. The second must receive the number the first did. Concurrency
    is what makes that property hard to see in the test above -- the
    interleaving decides whether a reissue happens at all -- and it is not
    needed to demonstrate it.
    """
    async with fixture.pool.acquire() as connection:
        with pytest.raises(_Rollback) as aborted:
            async with connection.transaction():
                allocated = await fixture.service.allocate_issue_number(
                    connection,
                    scope=SCOPE,
                    team_id=TEAM_ID,
                )

                raise _Rollback(allocated)

    assert aborted.value.number == SEED_COUNT + 1
    assert await _counter(fixture.connection, TEAM_ID) == SEED_COUNT

    async with fixture.pool.acquire() as connection:
        async with connection.transaction():
            reissued = await fixture.service.allocate_issue_number(
                connection,
                scope=SCOPE,
                team_id=TEAM_ID,
            )

    assert reissued == aborted.value.number


# --------------------------------------------------------------------------
# D. The control: the same harness, the forbidden allocator
# --------------------------------------------------------------------------


async def _create_issue_naively(fixture: Fixture, index: int) -> int:
    """`SELECT max(number) + 1`, then insert. The shape 005 forbids."""
    async with fixture.pool.acquire() as connection:
        async with connection.transaction():
            number = await connection.fetchval(NAIVE_ALLOCATE_SQL, TEAM_ID)

            await asyncio.sleep(LOCK_HOLD_SECONDS)

            await connection.execute(
                INSERT_ISSUE_SQL,
                WORKSPACE_ID,
                TEAM_ID,
                fixture.state_id,
                number,
                f"naive issue {index}",
            )

    return int(number)


async def test_the_forbidden_allocator_fails_under_the_same_concurrency(fixture):
    """Evidence that section A's success is not the harness being serial.

    This is a control, not a test of production code: nothing in `app/`
    allocates this way, and this test exists so that section A can be believed.
    If the tasks above were quietly running one after another, they would
    succeed with any allocator whatsoever -- including this one. They do not:
    run through the same pool, the same delay and the same transaction shape,
    `SELECT max(number) + 1` produces collisions that the unique constraint
    then refuses.

    Asserted as "at least one attempt failed" rather than an exact count. The
    exact number of survivors depends on how the server schedules the blocked
    inserts, which is not a property worth pinning; what is worth pinning is
    that the race is reachable at all from this harness.
    """
    results = await asyncio.gather(
        *[_create_issue_naively(fixture, index) for index in range(CONCURRENCY)],
        return_exceptions=True,
    )

    failures = [result for result in results if isinstance(result, BaseException)]

    assert failures, (
        "every naive allocation succeeded, so these tasks did not actually "
        "overlap -- which would mean the concurrency tests above proved nothing"
    )
    assert all(
        isinstance(failure, asyncpg.UniqueViolationError) for failure in failures
    ), f"unexpected failure kinds: {[type(f).__name__ for f in failures]}"


# --------------------------------------------------------------------------
# E. Allocation is refused across tenants
# --------------------------------------------------------------------------


async def test_allocation_refuses_a_team_that_does_not_exist(fixture):
    async with fixture.pool.acquire() as connection:
        async with connection.transaction():
            with pytest.raises(TeamNotFoundError):
                await fixture.service.allocate_issue_number(
                    connection,
                    scope=SCOPE,
                    team_id=MISSING_TEAM_ID,
                )


async def test_allocation_refuses_a_team_belonging_to_another_workspace(fixture):
    """The cross-tenant case, and the one that matters.

    A team id is a value a client can hold, guess or be handed. Without the
    workspace predicate in the UPDATE, this call would increment another
    tenant's counter and return one of its issue numbers -- a cross-tenant
    write performed by a method whose name says it only allocates.

    The counter of the foreign team is checked afterwards: the refusal has to
    be a refusal to *write*, not a write followed by an error.
    """
    await fixture.connection.execute(
        "INSERT INTO workspaces (id, slug, name) VALUES ($1, 'beta', 'Beta')",
        FOREIGN_WORKSPACE_ID,
    )
    await fixture.connection.execute(
        "INSERT INTO teams (id, workspace_id, name, key) VALUES ($1, $2, $3, $4)",
        FOREIGN_TEAM_ID,
        FOREIGN_WORKSPACE_ID,
        "Beta Core",
        "CORE",
    )

    async with fixture.pool.acquire() as connection:
        async with connection.transaction():
            with pytest.raises(TeamNotFoundError):
                await fixture.service.allocate_issue_number(
                    connection,
                    scope=SCOPE,
                    team_id=FOREIGN_TEAM_ID,
                )

    assert await _counter(fixture.connection, FOREIGN_TEAM_ID) == 0
