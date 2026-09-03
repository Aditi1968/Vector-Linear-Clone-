"""The parts of keyset pagination that a passing page walk does not prove.

`test_issue_pagination_db.py` walks four pages over a tie and checks the
ordering, the resume boundary and the page contents. Three properties survive
that walk unbroken, each demonstrated by a mutant that file accepts:

1. `ORDER BY created_at DESC` with the `id` tie-break deleted. On a freshly
   seeded ten-row table PostgreSQL has no statistics, estimates its way into
   an Index Only Scan on `issues_created_at_id_idx` -- which is
   `(created_at DESC, id DESC)` -- and hands back id-descending order that the
   query never asked for. Run `ANALYZE` first, or take index scans away, and
   the same mutant loses two rows out of ten. So the tie-break is proven by
   the plan, not by the test, and the test's own fixture is what keeps the
   plan favourable. This file therefore asserts the walk is identical under
   four plans, including one with index scans off.

2. A tie-break on any column that happens to agree with `id`. In that seed
   `updated_at` ascends with the id by construction, so
   `ORDER BY created_at DESC, updated_at DESC` returns the same ten rows in
   the same order -- while the resume predicate still compares `id`. Ordering
   on one key and resuming on another is precisely how a keyset walk starts
   dropping rows once the two disagree. Here every other column of a tied row
   -- `updated_at`, `title`, `priority` -- is ordered *against* the id, so
   only the id can produce the expected sequence.

3. A final page that is exactly full. Ten rows at page size three never
   returns exactly `first` rows, so `has_next_page = len(rows) >= first`
   passes there. At page size five the last page holds exactly five and the
   probe row decides the answer alone.

The seed is this file's own, deliberately hostile, and never reused from the
other file: its whole value is the columns that disagree.

Marked `db`: deselected by default, skipped when Docker is unreachable.
"""

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import asyncpg
import pytest

from app.domain.pagination import IssuePage
from app.repositories.issues import IssueRepository
from app.services.issues import IssueService


pytestmark = pytest.mark.db

MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "001_issues.sql"

INSERT = """
    INSERT INTO issues (
        id,
        title,
        description,
        priority,
        completed_at,
        created_at,
        updated_at
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7)
"""

# Three splits the ten rows 3/3/3/1, which is what the ordering assertions
# want. Five splits them 5/5, which is the only way to hand the service a
# final page holding exactly `first` rows.
PAGE_SIZE = 3
EXACT_PAGE_SIZE = 5

SEED_DAY = (2026, 1, 1)

UPDATED_BASE = datetime(2026, 2, 2, 8, 0, 0, tzinfo=timezone.utc)

# Anti-correlation constant: every derived column is built from
# `MIRROR - suffix`, so each one descends exactly as the id ascends.
MIRROR = 1000


def _at(hour: int, minute: int, second: int, microsecond: int = 0) -> datetime:
    return datetime(*SEED_DAY, hour, minute, second, microsecond, tzinfo=timezone.utc)


# Five rows carry this to the microsecond, so `created_at` alone cannot order
# them and cannot resume between them.
TIED = _at(12, 5, 0, 123456)


def _issue_id(suffix: int) -> UUID:
    """A UUID whose sort position follows `suffix`, in Python and in the server.

    PostgreSQL compares `uuid` bytewise over all sixteen bytes. Holding the
    first ten fixed and spelling the rest as twelve decimal digits makes the
    bytewise order the decimal order of `suffix`, so an expectation sorted in
    Python is the order the server produces.
    """
    return UUID(f"b7c8d9ea-0000-4000-8000-{suffix:012d}")


@dataclass(frozen=True, slots=True)
class SeedRow:
    suffix: int
    created_at: datetime
    # `priority` is spelled out per row rather than derived: it is capped at
    # 0-4 by issues_priority_range, so there is no arithmetic on the suffix
    # that both stays in range and descends as the id ascends. The premise
    # test below is what holds it to that, and it caught this being wrong.
    priority: int
    completed_at: datetime | None = None
    id: UUID = field(init=False)
    title: str = field(init=False)
    description: str | None = field(init=False)
    updated_at: datetime = field(init=False)

    def __post_init__(self) -> None:
        # `object.__setattr__` because the dataclass is frozen; these are
        # derived fields, not inputs, and deriving them here is what keeps
        # the anti-correlation impossible to break by editing one row.
        mirrored = MIRROR - self.suffix

        object.__setattr__(self, "id", _issue_id(self.suffix))
        object.__setattr__(self, "title", f"issue {mirrored:03d}")
        object.__setattr__(self, "description", f"row {self.suffix}")
        object.__setattr__(
            self,
            "updated_at",
            UPDATED_BASE + timedelta(seconds=mirrored),
        )


# Insertion order, and a deliberate scramble: not the expected order, not its
# reverse, not id order. Heap order cannot stand in for an ORDER BY.
SEED = (
    SeedRow(530, TIED, priority=2),
    SeedRow(50, _at(12, 1, 0), priority=4),
    SeedRow(900, _at(12, 9, 0, 500000), priority=0, completed_at=_at(13, 0, 0)),
    SeedRow(310, TIED, priority=4),
    SeedRow(700, _at(12, 7, 0, 1), priority=2),
    SeedRow(200, _at(12, 2, 0, 999999), priority=3),
    SeedRow(850, TIED, priority=0),
    SeedRow(640, TIED, priority=1),
    SeedRow(100, _at(12, 8, 0), priority=1),
    SeedRow(420, TIED, priority=3, completed_at=_at(13, 30, 0)),
)

TIED_ROWS = tuple(row for row in SEED if row.created_at == TIED)

# Sorted in Python from the literals above -- never by asking the code under
# test what the answer is.
EXPECTED = sorted(SEED, key=lambda row: (row.created_at, row.id), reverse=True)

EXPECTED_IDS = [row.id for row in EXPECTED]


@dataclass(frozen=True, slots=True)
class Plan:
    """A way of making the server reach the same rows by a different route."""

    name: str
    analyze: bool
    settings: dict[str, str]


# `enable_*` are planner preferences rather than prohibitions, so these ask
# for a different plan rather than guarantee one. That is enough: the claim
# under test is that the answer does not depend on which plan is chosen, and
# a suite that only ever exercises one plan cannot make it.
PLANS = (
    Plan("fresh table, planner's own choice", analyze=False, settings={}),
    Plan("table with statistics", analyze=True, settings={}),
    Plan(
        "index scans disabled",
        analyze=True,
        settings={
            "enable_indexscan": "off",
            "enable_indexonlyscan": "off",
            "enable_bitmapscan": "off",
        },
    ),
    Plan(
        "sequential scans disabled",
        analyze=True,
        settings={"enable_seqscan": "off"},
    ),
)


async def _seed(dsn: str, *, analyze: bool) -> None:
    """Rebuild `issues` holding exactly SEED.

    The container is shared for the session, so the table is dropped rather
    than assumed empty. `analyze` is the whole point of one of the plan
    cases: a table PostgreSQL has statistics for is planned differently from
    one it has just met, and a real table always has them.
    """
    connection = await asyncpg.connect(dsn)

    try:
        await connection.execute("DROP TABLE IF EXISTS issues")
        await connection.execute("DROP TABLE IF EXISTS schema_migrations")
        await connection.execute(MIGRATION.read_text(encoding="utf-8"))
        await connection.executemany(
            INSERT,
            [
                (
                    row.id,
                    row.title,
                    row.description,
                    row.priority,
                    row.completed_at,
                    row.created_at,
                    row.updated_at,
                )
                for row in SEED
            ],
        )

        if analyze:
            await connection.execute("ANALYZE issues")
    finally:
        await connection.close()


@asynccontextmanager
async def _service(dsn: str, settings: dict[str, str]):
    """The real service over the real repository, on a pool of one plan."""
    pool = await asyncpg.create_pool(
        dsn=dsn,
        min_size=1,
        max_size=2,
        server_settings=settings,
    )

    try:
        yield IssueService(pool=pool, repository=IssueRepository())
    finally:
        await pool.close()


async def _walk(service: IssueService, *, first: int) -> list[IssuePage]:
    """Every page, each fetched with the previous page's `endCursor`.

    Bounded so that a cursor which fails to advance fails the test instead of
    hanging the suite.
    """
    pages: list[IssuePage] = []
    after: str | None = None

    for _ in range(len(SEED) + 1):
        page = await service.list(first=first, after=after)
        pages.append(page)

        if not page.has_next_page:
            return pages

        after = page.end_cursor

    raise AssertionError(
        f"pagination did not terminate: {len(pages)} pages of {first} over "
        f"{len(SEED)} rows"
    )


def test_the_seed_orders_every_other_column_against_the_id_inside_the_tie():
    """The premise the two database tests rest on, checked not assumed.

    If a later edit made any of these columns agree with the id ordering
    again, a tie-break on that column would become invisible and the walk
    below would go back to proving less than it claims.
    """
    assert len(TIED_ROWS) > PAGE_SIZE

    by_id = [row.suffix for row in sorted(TIED_ROWS, key=lambda r: r.id, reverse=True)]

    for label, key in (
        ("updated_at", lambda row: row.updated_at),
        ("title", lambda row: row.title),
        ("priority", lambda row: row.priority),
    ):
        by_column = [row.suffix for row in sorted(TIED_ROWS, key=key, reverse=True)]

        assert by_column == list(reversed(by_id)), (
            f"{label} must order the tie against the id, or a tie-break on "
            f"{label} would pass this file: {by_column} vs {by_id}"
        )

    # A boundary inside the tie, so `created_at` cannot decide where a page
    # resumes, and a whole page inside it, so that page is ordered by the
    # tie-break alone.
    boundaries = [
        index
        for index in range(PAGE_SIZE, len(EXPECTED), PAGE_SIZE)
        if EXPECTED[index - 1].created_at == EXPECTED[index].created_at
    ]

    assert boundaries

    page_two = EXPECTED[PAGE_SIZE : 2 * PAGE_SIZE]

    assert all(row.created_at == TIED for row in page_two)

    # And the page size that makes the final page exactly full.
    assert len(SEED) % EXACT_PAGE_SIZE == 0
    assert len(SEED) // EXACT_PAGE_SIZE == 2


@pytest.mark.parametrize("plan", PLANS, ids=lambda plan: plan.name)
async def test_the_paged_walk_is_identical_under_every_query_plan(postgres_dsn, plan):
    """Assertion: the ordering is the query's, not the plan's.

    An index whose declared order matches the sort can deliver that order
    even when the SQL stopped asking for it -- and `issues_created_at_id_idx`
    is exactly `(created_at DESC, id DESC)`. Walking the same rows with index
    scans disabled forces the server to sort by what the query actually says,
    which is the only way this suite can tell the two apart.
    """
    await _seed(postgres_dsn, analyze=plan.analyze)

    async with _service(postgres_dsn, plan.settings) as service:
        pages = await _walk(service, first=PAGE_SIZE)

    walked = [node.id for page in pages for node in page.nodes]

    assert walked == EXPECTED_IDS, f"wrong order under plan: {plan.name}"

    # Stated separately from the ordering: a plan change that dropped or
    # repeated a row would otherwise be reported only as a mis-ordering.
    assert len(walked) == len(set(walked)) == len(SEED)


async def test_a_page_that_is_exactly_full_does_not_announce_another_page(
    postgres_dsn,
):
    """Assertion: `has_next_page` comes from the probe row, not the page size.

    Ten rows at page size five make the final page exactly full, so the only
    thing separating "one more page" from "that was the last one" is whether
    the extra row came back. At page size three -- the only size the sibling
    file walks -- no page ever returns exactly `first` rows, so an
    implementation reading `>=` instead of `>` is never asked the question.
    """
    await _seed(postgres_dsn, analyze=False)

    async with _service(postgres_dsn, {}) as service:
        pages = await _walk(service, first=EXACT_PAGE_SIZE)

        assert [len(page.nodes) for page in pages] == [
            EXACT_PAGE_SIZE,
            EXACT_PAGE_SIZE,
        ]

        first_page, final = pages

        assert first_page.has_next_page is True
        assert final.has_next_page is False

        # The walk stopped because the row was not there, not because the
        # rows ran out on a later fetch: asking again returns nothing.
        assert [node.id for page in pages for node in page.nodes] == EXPECTED_IDS

        beyond = await service.list(first=EXACT_PAGE_SIZE, after=final.end_cursor)

    assert beyond.nodes == []
    assert beyond.has_next_page is False
