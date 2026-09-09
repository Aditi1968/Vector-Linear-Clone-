"""Workspace isolation for issues, asked of a real PostgreSQL 18.

Every other tenancy test in this suite reads the SQL the repository sends
against a fake connection. That proves the statement carries a
`workspace_id` predicate; it cannot prove the predicate *excludes* anything,
because a fake connection agrees with whatever it is handed. The question
this file exists for is the one only a server answers: with two workspaces
holding rows at once, does one workspace's read ever return the other's row.

So the dataset is two tenants interleaved rather than blocked. Their
`created_at` values alternate down the timeline, and one row from each
shares a timestamp to the microsecond with the other's -- with the *other*
tenant's id sorting lower, so that a query missing its tenant predicate
resumes into the wrong workspace rather than merely past it. Blocked data
(all of A newer than all of B) would let a leaking first page still look
right, which is exactly the shape a seed must not have.

Three claims, in the order they are cheap to break:

  * a read of another tenant's id answers exactly as a read of an id that
    exists nowhere, so the resolver cannot be used to probe for issues;
  * a page walk stays inside one workspace, boundary and tie included, and a
    cursor minted in one workspace selects nothing belonging to another;
  * a write lands in the workspace it was given, and a team from a different
    workspace is refused by the server rather than written.

There is no update path to test. `IssueRepository` exposes get_by_id, create
and list and nothing else today, so "workspace A cannot update workspace B's
issue" has no operation to assert against; when one is added, its isolation
belongs in this file next to the three above.

Two disciplines are borrowed from tests/test_issue_pagination_db.py. Ids come
from a suffix scheme whose hex order is its decimal order, so Python and
PostgreSQL sort the seed alike; and expected values are computed in Python
from the seed literals, never by re-running the query under test.

Marked `db`: deselected by default, skipped when Docker is unreachable.
Nothing here touches DATABASE_URL or Neon; the only server it speaks to is
the throwaway container `postgres_dsn` starts.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import asyncpg
import pytest

from app.domain.errors import ValidationError
from app.domain.pagination import IssuePage
from app.domain.tenancy import WorkspaceScope
from app.repositories.issues import IssueRepository
from app.repositories.teams import TeamRepository
from app.services.issues import IssueService
from app.services.teams import TeamService

from tests.conftest import (
    apply_all_migrations,
    reset_schema,
    seed_workflow_states,
)


pytestmark = pytest.mark.db

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"
MIGRATION_001 = MIGRATIONS_DIR / "001_issues.sql"
MIGRATION_002 = MIGRATIONS_DIR / "002_tenancy.sql"

# Workspace A is the tenant 002 seeds, named in that file as literals
# precisely so a test can assert against a constant instead of querying for
# the value it is about to check. Workspace B is this file's own, because
# isolation is not expressible with one tenant.
WORKSPACE_A = UUID("00000000-0000-7000-8000-000000000001")
TEAM_A = UUID("00000000-0000-7000-8000-000000000002")

WORKSPACE_B = UUID("00000000-0000-7000-8000-0000000000e1")
TEAM_B = UUID("00000000-0000-7000-8000-0000000000e2")

SCOPE_A = WorkspaceScope(workspace_id=WORKSPACE_A)
SCOPE_B = WorkspaceScope(workspace_id=WORKSPACE_B)

# An id belonging to no workspace at all. The comparison that matters is
# between this answer and the answer for another tenant's real id: if they
# differ in any way a client can see, the difference reports that someone
# else's issue exists.
ABSENT_ISSUE_ID = UUID("a1b2c3d4-0000-4000-8000-000000000999")

# Small on purpose: at three rows per workspace a wrong page is readable at a
# glance, and page size two still makes two pages with a boundary inside the
# cross-tenant tie.
PAGE_SIZE = 2

INSERT_WORKSPACE = """
    INSERT INTO workspaces (id, slug, name)
    VALUES ($1, $2, $3)
"""

INSERT_TEAM = """
    INSERT INTO teams (id, workspace_id, name, key)
    VALUES ($1, $2, $3, $4)
"""

# Seeded directly rather than through the service, because these rows exist
# to be *paged over* -- their created_at values are chosen to make the keyset
# order falsifiable, which an application insert would not let a test pick.
#
# 005 made `number` and `workflow_state_id` NOT NULL, so both are supplied
# here too. The number comes from a per-team counter the fixture keeps, and
# the state is looked up by category from the issue's own team -- the same
# rule `TeamRepository.find_default_workflow_state_id` uses, written as a
# subquery so the seed cannot point an issue at another team's board.
INSERT_ISSUE = """
    INSERT INTO issues (
        id,
        workspace_id,
        team_id,
        number,
        workflow_state_id,
        title,
        description,
        priority,
        completed_at,
        created_at,
        updated_at
    )
    VALUES (
        $1, $2, $3, $10,
        (
            SELECT id
            FROM workflow_states
            WHERE workspace_id = $2 AND team_id = $3 AND type = 'unstarted'
            ORDER BY position, id
            LIMIT 1
        ),
        $4, $5, $6, $7, $8, $9
    )
"""


def _at(hour: int, minute: int, second: int, microsecond: int = 0) -> datetime:
    return datetime(2026, 1, 1, hour, minute, second, microsecond, tzinfo=timezone.utc)


# The timestamp one row of each workspace shares, microseconds included. A
# tie is where `created_at` alone cannot decide a boundary, so it is where a
# missing tenant predicate stops being a matter of degree.
TIED = _at(12, 4, 0, 123456)


def _issue_id(suffix: int) -> UUID:
    """A UUID whose sort position follows `suffix`.

    PostgreSQL compares `uuid` bytewise across all sixteen bytes rather than
    as text, so a test may not simply assume Python's ordering carries over.
    This scheme makes the two agree by construction: every id shares the same
    leading ten bytes, and the trailing six are twelve digits that are all
    0-9, so their hex ordering is the decimal ordering of `suffix`.
    """
    return UUID(f"a1b2c3d4-0000-4000-8000-{suffix:012d}")


@dataclass(frozen=True, slots=True)
class SeedRow:
    suffix: int
    workspace_id: UUID
    team_id: UUID
    created_at: datetime

    @property
    def id(self) -> UUID:
        return _issue_id(self.suffix)

    @property
    def title(self) -> str:
        return f"issue {self.suffix:03d}"


# Interleaved, not blocked. Reading the whole table newest-first gives
# A, B, A, B, B, A -- so a listing that lost its workspace predicate returns
# the other tenant's row in its very first page rather than somewhere past
# the end.
#
# The tie is the sharp edge. Suffixes 30 (B) and 40 (A) share TIED, and the
# B row's id sorts LOWER, which is what puts it immediately after the A row
# in a global ordering. A page of A resuming from suffix 40 therefore returns
# suffix 30 first if the tenant predicate is missing from the cursor branch --
# the one branch where an extra WHERE term is easy to forget, because the
# cursor comparison already looks like a complete WHERE clause.
SEED = (
    SeedRow(
        suffix=10, workspace_id=WORKSPACE_A, team_id=TEAM_A, created_at=_at(12, 1, 0)
    ),
    SeedRow(
        suffix=20, workspace_id=WORKSPACE_B, team_id=TEAM_B, created_at=_at(12, 2, 0)
    ),
    SeedRow(suffix=30, workspace_id=WORKSPACE_B, team_id=TEAM_B, created_at=TIED),
    SeedRow(suffix=40, workspace_id=WORKSPACE_A, team_id=TEAM_A, created_at=TIED),
    SeedRow(
        suffix=50, workspace_id=WORKSPACE_B, team_id=TEAM_B, created_at=_at(12, 5, 0)
    ),
    SeedRow(
        suffix=60, workspace_id=WORKSPACE_A, team_id=TEAM_A, created_at=_at(12, 6, 0)
    ),
)


def _expected(workspace_id: UUID) -> list[SeedRow]:
    """One workspace's rows, newest first, computed from the seed literals.

    `reverse=True` over the (created_at, id) tuple states the repository's
    `ORDER BY created_at DESC, id DESC` independently. A test whose expected
    answer came out of the code under test could not fail when that code is
    wrong.
    """
    return sorted(
        (row for row in SEED if row.workspace_id == workspace_id),
        key=lambda row: (row.created_at, row.id),
        reverse=True,
    )


EXPECTED_A = _expected(WORKSPACE_A)
EXPECTED_B = _expected(WORKSPACE_B)

# The whole table newest-first: what a query with no tenant predicate returns,
# and therefore what every assertion below is really being compared against.
EXPECTED_ALL = sorted(SEED, key=lambda row: (row.created_at, row.id), reverse=True)


@dataclass(frozen=True, slots=True)
class Tenanted:
    service: IssueService
    connection: asyncpg.Connection


@pytest.fixture
async def tenanted(postgres_dsn):
    """001 then 002 through the runner, two tenants, a real pool.

    The schema is built the way tests/test_workspace_resolution.py's
    `resolved` fixture builds it, and for the same reason: hand-written DDL
    would be a second definition of the schema, and the claim under test is
    about the one the migrations produce.

    Every table is dropped first. The container is shared for the whole
    session, so a table left behind by an earlier file would make 002's
    CREATE TABLE fail here with a failure that has nothing to do with this
    test.

    Ids and timestamps are supplied explicitly rather than defaulted:
    `uuidv7()` and `now()` both ascend with insertion order, and this
    dataset's whole value is that its ids and timestamps disagree with it.

    The raw connection is yielded alongside the service because two
    assertions have to read `issues.workspace_id` -- a column the repository
    deliberately never returns -- to say where a row actually landed.
    """
    connection = await asyncpg.connect(postgres_dsn)

    try:
        # The whole chain, not 001 and 002. These suites create issues
        # through the application, and 005 made `issues.number` and
        # `issues.workflow_state_id` NOT NULL and put the states they
        # reference in a table of their own -- so an insert needs every
        # migration, not the two that introduced the columns it names.
        # `reset_schema` drops whatever is there rather than a list that
        # goes stale as the foreign-key graph grows.
        await reset_schema(connection)
        await apply_all_migrations(connection)

        await connection.execute(INSERT_WORKSPACE, WORKSPACE_B, "acme", "Acme")
        await connection.execute(INSERT_TEAM, TEAM_B, WORKSPACE_B, "Acme Core", "ACME")

        # 005 seeded boards for the teams that existed when it ran; this team
        # was created after it, so it needs one before an issue can be filed
        # against it.
        await seed_workflow_states(connection, WORKSPACE_B, TEAM_B)

        # One counter per team, so numbers are unique within a team --
        # `issues_team_number_key` is a UNIQUE (team_id, number) and would
        # refuse a second row sharing one. The value is not asserted
        # anywhere; it exists because the column is NOT NULL.
        numbers: dict[UUID, int] = {}

        def next_number(team_id: UUID) -> int:
            numbers[team_id] = numbers.get(team_id, 0) + 1

            return numbers[team_id]

        await connection.executemany(
            INSERT_ISSUE,
            [
                (
                    row.id,
                    row.workspace_id,
                    row.team_id,
                    row.title,
                    None,
                    0,
                    None,
                    row.created_at,
                    row.created_at,
                    next_number(row.team_id),
                )
                for row in SEED
            ],
        )

        # Without statistics the planner's default guess favours an index
        # scan whose declared order can hand back rows already sorted, which
        # would let a query that lost part of its ORDER BY still look right.
        # Every real table has statistics; this one should too.
        # Every team's counter moved to its high-water mark, exactly as 005
        # does after backfilling numbers. Without this the first allocation
        # on a team returns 1 and collides with a seeded issue -- which is a
        # fixture defect, not a product one, and reads like neither.
        await connection.execute(
            """
            UPDATE teams
            SET issue_counter = coalesce(
                (SELECT max(number) FROM issues WHERE issues.team_id = teams.id),
                0
            )
            """
        )

        await connection.execute("ANALYZE issues")

        pool = await asyncpg.create_pool(dsn=postgres_dsn, min_size=1, max_size=2)

        try:
            yield Tenanted(
                service=IssueService(
                    pool=pool,
                    repository=IssueRepository(),
                    teams=TeamService(pool=pool, repository=TeamRepository()),
                ),
                connection=connection,
            )
        finally:
            await pool.close()
    finally:
        await connection.close()


async def _walk(service: IssueService, scope: WorkspaceScope) -> list[IssuePage]:
    """Every page for one workspace, each fetched with the previous cursor.

    Bounded rather than `while True`: a cursor that fails to advance has to
    fail this test, not hang the suite.
    """
    pages: list[IssuePage] = []
    after: str | None = None

    for _ in range(len(SEED) + 1):
        page = await service.list(scope=scope, first=PAGE_SIZE, after=after)
        pages.append(page)

        if not page.has_next_page:
            return pages

        after = page.end_cursor

    raise AssertionError(
        f"pagination did not terminate: {len(pages)} pages of {PAGE_SIZE} over "
        f"{len(SEED)} rows"
    )


def test_the_two_workspaces_interleave_and_share_a_timestamp():
    """The premise every assertion below rests on, checked not assumed.

    If a later seed edit blocked the tenants apart, or moved the tie so that
    both tied rows belonged to one workspace, the tests would still pass
    while a leaking query looked correct. This is the guard against that.
    """
    # The whole-table order, owner by owner. Written out rather than probed
    # for "some adjacent pair differs", because what matters is that the
    # alternation starts at the very first pair: a listing that lost its
    # predicate then returns a foreign row inside its first page, at any page
    # size, rather than somewhere past the end where a short test never looks.
    assert [row.workspace_id for row in EXPECTED_ALL] == [
        WORKSPACE_A,
        WORKSPACE_B,
        WORKSPACE_A,
        WORKSPACE_B,
        WORKSPACE_B,
        WORKSPACE_A,
    ]

    tied = [row for row in SEED if row.created_at == TIED]

    assert len(tied) == 2
    assert {row.workspace_id for row in tied} == {WORKSPACE_A, WORKSPACE_B}

    a_tied = next(row for row in tied if row.workspace_id == WORKSPACE_A)
    b_tied = next(row for row in tied if row.workspace_id == WORKSPACE_B)

    # The B row sorts immediately after the A row globally, so an unscoped
    # resume from the A row returns the B row first.
    assert b_tied.id < a_tied.id
    assert EXPECTED_ALL[EXPECTED_ALL.index(a_tied) + 1] is b_tied

    # Both workspaces need more rows than a page holds, or no walk here ever
    # asks the cursor branch anything.
    assert len(EXPECTED_A) > PAGE_SIZE
    assert len(EXPECTED_B) > PAGE_SIZE


@pytest.mark.parametrize(
    ("scope", "own", "foreign"),
    [
        (SCOPE_A, EXPECTED_A, EXPECTED_B),
        (SCOPE_B, EXPECTED_B, EXPECTED_A),
    ],
    ids=["workspace A", "workspace B"],
)
async def test_a_workspace_reads_its_own_issues_and_not_the_others(
    tenanted, scope, own, foreign
):
    """Both halves in one test, because either alone is satisfiable wrongly.

    A repository that returned None for everything passes the isolation half.
    One that ignored the scope passes the visibility half. Only the pair
    describes a working lookup.
    """
    for row in own:
        entity = await tenanted.service.get_by_id(scope=scope, issue_id=row.id)

        assert entity is not None
        assert entity.id == row.id
        assert entity.title == row.title

    for row in foreign:
        assert await tenanted.service.get_by_id(scope=scope, issue_id=row.id) is None


@pytest.mark.parametrize(
    ("scope", "foreign"),
    [(SCOPE_A, EXPECTED_B), (SCOPE_B, EXPECTED_A)],
    ids=["workspace A", "workspace B"],
)
async def test_another_tenants_id_is_indistinguishable_from_an_absent_one(
    tenanted, scope, foreign
):
    """The property that keeps ids from being an existence oracle.

    An id that belongs to another workspace and an id that belongs to nobody
    must produce the same answer. Any observable difference -- a different
    return, a raised error, a distinguishable message -- tells a caller
    holding a guessed or leaked id that the issue is real and simply not
    theirs, which is the fact the scoping exists to withhold.
    """
    absent = await tenanted.service.get_by_id(
        scope=scope,
        issue_id=ABSENT_ISSUE_ID,
    )

    assert absent is None

    for row in foreign:
        assert await tenanted.service.get_by_id(scope=scope, issue_id=row.id) is absent


@pytest.mark.parametrize(
    ("scope", "expected"),
    [(SCOPE_A, EXPECTED_A), (SCOPE_B, EXPECTED_B)],
    ids=["workspace A", "workspace B"],
)
async def test_a_listing_holds_one_workspaces_issues_in_order(
    tenanted, scope, expected
):
    """One page wide enough for everything: contents and order in one claim.

    Asserted as a sequence rather than a set, because the ordering is what a
    later page walk depends on, and against the whole-table ordering, which
    is what a query with no tenant predicate would have returned.
    """
    page = await tenanted.service.list(scope=scope, first=len(SEED), after=None)

    listed = [node.id for node in page.nodes]

    assert listed == [row.id for row in expected]
    assert listed != [row.id for row in EXPECTED_ALL]
    assert page.has_next_page is False


@pytest.mark.parametrize(
    ("scope", "expected"),
    [(SCOPE_A, EXPECTED_A), (SCOPE_B, EXPECTED_B)],
    ids=["workspace A", "workspace B"],
)
async def test_a_paged_walk_stays_inside_its_workspace(tenanted, scope, expected):
    """The cursor branch, walked across the tie it shares with the other tenant.

    The first page is served by the no-cursor statement and every later one by
    the cursor statement, so a walk is the only thing that exercises both. The
    boundary falls inside the cross-tenant tie by construction, which is where
    a missing predicate returns the other workspace's row rather than skipping
    past it.
    """
    pages = await _walk(tenanted.service, scope)

    walked = [node.id for page in pages for node in page.nodes]

    assert walked == [row.id for row in expected]

    # Stated separately from the ordering: a leak that also dropped or
    # repeated a row would otherwise be reported only as a mis-ordering.
    assert len(walked) == len(set(walked)) == len(expected)

    foreign = {row.id for row in SEED if row.workspace_id != scope.workspace_id}

    assert not foreign & set(walked)


async def test_a_cursor_from_one_workspace_cannot_resume_in_another(tenanted):
    """Cursors are opaque, unauthenticated, and not tenant identity.

    A cursor is Base64 over JSON: a client can read one, write one, and hand
    one back against a different workspace. This asserts what that buys them,
    which is nothing. The cursor is only a position in an ordering; the
    workspace comes from the call, so replaying A's cursor under B's scope
    returns B's rows from that position and never A's.
    """
    first_page = await tenanted.service.list(scope=SCOPE_A, first=PAGE_SIZE, after=None)

    assert first_page.end_cursor is not None

    replayed = await tenanted.service.list(
        scope=SCOPE_B,
        first=len(SEED),
        after=first_page.end_cursor,
    )

    returned = [node.id for node in replayed.nodes]
    b_ids = {row.id for row in EXPECTED_B}

    assert returned
    assert set(returned) <= b_ids

    # And it is a real position, not an empty answer that would satisfy the
    # subset check trivially: the rows are exactly B's that sort after the
    # cursor's (created_at, id).
    boundary = EXPECTED_A[PAGE_SIZE - 1]

    assert returned == [
        row.id
        for row in EXPECTED_B
        if (row.created_at, row.id) < (boundary.created_at, boundary.id)
    ]


async def test_a_created_issue_lands_in_the_workspace_it_was_given(tenanted):
    """Where the row went, read from the column the repository never returns.

    `IssueEntity` carries no workspace, so a create that wrote the wrong
    tenant would return an entity indistinguishable from a correct one. The
    only witness is the stored row, so this reads `workspace_id` and
    `team_id` back through the raw connection, and then confirms the row is
    visible to exactly one of the two workspaces.
    """
    entity = await tenanted.service.create(
        scope=SCOPE_B,
        team_id=TEAM_B,
        title="Filed in B",
        description=None,
        priority=1,
    )

    stored = await tenanted.connection.fetchrow(
        "SELECT workspace_id, team_id FROM issues WHERE id = $1",
        entity.id,
    )

    assert stored["workspace_id"] == WORKSPACE_B
    assert stored["team_id"] == TEAM_B

    assert await tenanted.service.get_by_id(scope=SCOPE_B, issue_id=entity.id) == entity
    assert await tenanted.service.get_by_id(scope=SCOPE_A, issue_id=entity.id) is None

    listed_in_a = await tenanted.service.list(
        scope=SCOPE_A, first=len(SEED), after=None
    )

    assert entity.id not in [node.id for node in listed_in_a.nodes]


async def test_filing_against_another_workspaces_team_is_refused(tenanted):
    """The composite foreign key, exercised through the service that relies on it.

    `issues_team_fk` references `teams (workspace_id, id)`, so this pair
    describes an issue in workspace A filed against a team that belongs to
    workspace B -- the exact shape two single-column foreign keys would have
    accepted. The repository does no pre-check on purpose; the server is the
    one place the rule cannot be raced.

    The count is asserted afterwards because a violation inside the service's
    transaction must leave nothing behind. A rejected insert that still
    committed would be the cross-tenant write this constraint exists to
    prevent, arriving with an error message attached.

    The service now refuses this one statement earlier than the foreign key
    does, and that is worth being precise about. Creating an issue resolves
    the team's default workflow state first, and that lookup is scoped by
    workspace, so a team from another tenant resolves to nothing before any
    INSERT is attempted. That is not a substitute for the constraint and must
    not become one: the lookup is needed for its value, not as a check, and
    the second half of this test asserts the server still refuses the pair on
    its own.

    What that early refusal *is* changed here. It used to raise
    `TeamNotFoundError`, which nothing above the service mapped, so a client
    sending another workspace's team id got "Internal server error" -- found
    by doing exactly that against a running stack. It is now a
    `ValidationError` naming `teamId`, and this test asserts the field and
    the code rather than just the type: the message must be identical to the
    one a team that exists NOWHERE produces, or the error becomes a probe for
    which team ids exist in other tenants.
    """
    before = await tenanted.connection.fetchval("SELECT count(*) FROM issues")

    with pytest.raises(ValidationError) as foreign_team:
        await tenanted.service.create(
            scope=SCOPE_A,
            team_id=TEAM_B,
            title="Filed across tenants",
            description=None,
            priority=1,
        )

    assert [(i.field, i.code) for i in foreign_team.value.issues] == [
        ("teamId", "NOT_FOUND")
    ]

    # The same request with a team id belonging to nobody at all. The two
    # answers must be indistinguishable; a client that can tell them apart
    # can enumerate other workspaces' teams one guess at a time.
    with pytest.raises(ValidationError) as absent_team:
        await tenanted.service.create(
            scope=SCOPE_A,
            team_id=_issue_id(997),
            title="Filed against nothing",
            description=None,
            priority=1,
        )

    assert [
        (i.field, i.code, i.message) for i in absent_team.value.issues
    ] == [(i.field, i.code, i.message) for i in foreign_team.value.issues]

    assert await tenanted.connection.fetchval("SELECT count(*) FROM issues") == before

    # And the constraint itself, with the application stepped over entirely.
    # Without this, an implementation that dropped `issues_team_fk` and kept
    # only the scoped lookup would pass the assertion above -- and would then
    # accept exactly this row from any caller that reached the repository
    # directly, or lost the race between the lookup and the insert.
    state_id = await tenanted.connection.fetchval(
        "SELECT id FROM workflow_states WHERE team_id = $1 LIMIT 1", TEAM_B
    )

    assert state_id is not None

    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await tenanted.connection.execute(
            """
            INSERT INTO issues (
                id, workspace_id, team_id, number, workflow_state_id,
                title, description, priority, created_at, updated_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, NULL, 1, $7, $7)
            """,
            _issue_id(998),
            WORKSPACE_A,
            TEAM_B,
            999,
            state_id,
            "Filed across tenants, straight to SQL",
            _at(12, 9, 0),
        )
    assert await tenanted.connection.fetchval("SELECT count(*) FROM issues") == before
