"""Keyset pagination over issues, executed by a real PostgreSQL 18.

Every other pagination test in this suite talks to a fake connection: they
pin the SQL text the repository sends and the PageInfo the service builds
from whatever rows it is handed. Neither of those is evidence that the query
*orders and resumes correctly*, because no server ever ran it. The one
question that only a real server can answer is whether
`WHERE (created_at, id) < ($1, $2) ORDER BY created_at DESC, id DESC` walks a
table exactly once -- and the case where it can go wrong is a group of rows
sharing a `created_at`, where `created_at` alone cannot decide the boundary.

So the dataset is built around that: ten rows, five of them sharing one
timestamp to the microsecond, arranged so that a page boundary falls *inside*
the tie rather than at its edge, with tied rows left on both sides of it. A
`created_at`-only predicate would silently drop the tied rows on the far side
of that boundary, which is exactly the bug this file exists to catch.

The tie is deliberately wider than one page, so that a whole page is ordered
by the id tie-break alone and the page that resumes from inside the tie still
holds two tied rows. That is what makes one assertion sensitive to the
resume predicate and to the ORDER BY at the same time.

Two rules keep it honest. The expected ordering is computed by sorting the
seed data in Python, never by re-running the query under test. And the rows
are inserted in an order that is neither the expected output order nor its
reverse, so heap order cannot stand in for a working ORDER BY.

Marked `db`: deselected by default, skipped when Docker is unreachable.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from operator import attrgetter
from pathlib import Path
from uuid import UUID

import asyncpg
import pytest

from app.domain.pagination import IssuePage, decode_issue_cursor, encode_issue_cursor
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

# Small on purpose. At three rows a wrong page is readable at a glance, and
# ten rows still make four pages -- three full and one partial.
PAGE_SIZE = 3

SEED_DAY = (2026, 1, 1)

# `updated_at` deliberately orders unlike `created_at`, so a query that
# sorted or resumed on the wrong timestamp column produces a different
# sequence instead of an accidentally identical one.
#
# Outside the tie its offset is `DECOY_SPAN - suffix`, which is enough to
# keep it disagreeing with `created_at`. Inside the tie it is overridden per
# row, and so are `title` and `priority` -- see TIED_DECOYS.
UPDATED_BASE = datetime(2026, 2, 2, 8, 0, 0, tzinfo=timezone.utc)
DECOY_SPAN = 1000


def _at(hour: int, minute: int, second: int, microsecond: int = 0) -> datetime:
    return datetime(*SEED_DAY, hour, minute, second, microsecond, tzinfo=timezone.utc)


# The tie. Five rows carry this exact value, microseconds included -- wider
# than PAGE_SIZE, which is what puts a whole page inside the tie and leaves
# tied rows on both sides of a page boundary. The microseconds also prove
# that the cursor's isoformat round trip does not quietly truncate what
# PostgreSQL stored.
TIED = _at(12, 5, 0, 123456)


def _issue_id(suffix: int) -> UUID:
    """A UUID whose sort position follows `suffix`.

    PostgreSQL compares `uuid` bytewise across all sixteen bytes, not as
    text, so a test may not simply assume Python's ordering carries over.
    This scheme makes the two agree by construction: every id shares the same
    leading ten bytes, and the trailing six are twelve digits that are all
    0-9, so their hex ordering is the decimal ordering of `suffix`. That is
    an argument, not a proof, which is why
    test_the_seed_uuids_sort_in_postgres_the_way_python_sorts_them puts it to
    the server rather than leaving it in this comment.
    """
    return UUID(f"a1b2c3d4-0000-4000-8000-{suffix:012d}")


@dataclass(frozen=True, slots=True)
class SeedRow:
    id: UUID
    title: str
    description: str | None
    priority: int
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


def _row(
    suffix: int,
    created_at: datetime,
    *,
    description: str | None = None,
    priority: int = 0,
    completed_at: datetime | None = None,
    title_number: int | None = None,
    updated_offset: int | None = None,
) -> SeedRow:
    """One seed row. Tied rows override the decoy columns; see TIED_DECOYS.

    The number in the title is never the id suffix -- a title that tracked
    the id would be one more column able to stand in for the tie-break. It is
    zero-padded so string order stays numeric order. Read failure output by
    id, not by title.
    """
    if title_number is None:
        title_number = DECOY_SPAN - suffix

    if updated_offset is None:
        updated_offset = DECOY_SPAN - suffix

    return SeedRow(
        id=_issue_id(suffix),
        title=f"issue {title_number:03d}",
        description=description,
        priority=priority,
        completed_at=completed_at,
        created_at=created_at,
        updated_at=UPDATED_BASE + timedelta(seconds=updated_offset),
    )


# Written in insertion order, which is a deliberate scramble: it is neither
# the expected output order (30, 10, 70, 90, 80, 60, 50, 20, 40, 100) nor its
# reverse, and it is not sorted by id either. Nothing about the physical order
# of the heap can make a broken ORDER BY look right.
#
# Note the id suffixes against the timestamps: id order alone (100, 90, 80,
# 70, ...) is nothing like the expected order, so a query that dropped
# `created_at` from the sort would also be caught.
#
# TIED_DECOYS. Inside the tie, `title`, `priority` and `updated_at` carry
# hand-picked values rather than anything derived from the suffix, because
# every column that is a total order over the tie is a candidate tie-break a
# wrong query could sort by. Such a column has to disagree with `id` or it
# stands in for the tie-break silently: substituting it into the ORDER BY
# leaves every assertion in this file passing.
#
# Disagreeing is not enough, and this is the part that is easy to get wrong.
# Over a set of five, a total order is the identity, the reversal, or neither.
# These three columns first ascended with the id -- the identity -- so each
# was inverted; that killed `<column> DESC` as a proxy and made
# `<column> ASC` into one, trading a false negative for its mirror image.
# What is needed is "neither": a scramble. So the DESC order of each is:
#
#     id       90  80  60  50  20   <- what the tie-break must produce
#     title    80  50  90  20  60
#     priority 60  90  20  80  50
#     updated  20  90  50  60  80
#
# A different permutation per column, since three decoys sharing one order
# would be the same bug at smaller scale. None is any other's reverse.
# test_no_other_seed_column_orders_the_tie_the_way_id_does checks all of it,
# in both directions, so a later seed edit cannot quietly undo it.
#
# Tied priorities use 0-4, the full range migration 001's
# CHECK (priority BETWEEN 0 AND 4) allows, which is exactly five distinct
# values for five tied rows. Tied title/updated numbers sit in a 140-180 band
# well clear of the 900s the suffix formula gives everything else.
SEED = (
    _row(60, TIED, priority=4, title_number=140, updated_offset=150),
    _row(100, _at(12, 1, 0), description="oldest row of all"),
    _row(30, _at(12, 9, 0, 500000), priority=4, completed_at=_at(13, 0, 0)),
    _row(
        20,
        TIED,
        priority=2,
        title_number=150,
        updated_offset=180,
        description="tied, lowest id",
    ),
    _row(70, _at(12, 7, 0, 1), priority=2),
    _row(40, _at(12, 2, 0, 999999)),
    _row(
        90,
        TIED,
        priority=3,
        title_number=160,
        updated_offset=170,
        description="tied, highest id",
    ),
    _row(
        80,
        TIED,
        priority=1,
        title_number=180,
        updated_offset=140,
        description="tied, second highest id",
    ),
    _row(
        10,
        _at(12, 8, 0),
        description="second newest timestamp but the lowest id",
    ),
    _row(
        50,
        TIED,
        priority=0,
        title_number=170,
        updated_offset=160,
        completed_at=_at(13, 30, 0),
    ),
)

SEED_IDS = frozenset(row.id for row in SEED)
TIED_IDS = frozenset(row.id for row in SEED if row.created_at == TIED)

# The whole point of the file: the expectation is derived from the seed data,
# by sorting it, and never from the repository. A test whose expected answer
# comes out of the code under test cannot fail when that code is wrong.
# `reverse=True` over the (created_at, id) tuple is `ORDER BY created_at DESC,
# id DESC` -- the same ordering, stated independently.
EXPECTED = sorted(SEED, key=lambda row: (row.created_at, row.id), reverse=True)

EXPECTED_IDS = [row.id for row in EXPECTED]

EXPECTED_PAGES = [
    EXPECTED[start : start + PAGE_SIZE] for start in range(0, len(EXPECTED), PAGE_SIZE)
]

# Page boundaries that fall between two rows holding the same `created_at`.
# Computed from the dataset rather than asserted by eye, so that editing SEED
# or PAGE_SIZE cannot silently move the boundary out of the tie and leave the
# file passing while testing nothing.
TIED_BOUNDARIES = [
    index
    for index in range(PAGE_SIZE, len(EXPECTED), PAGE_SIZE)
    if EXPECTED[index - 1].created_at == EXPECTED[index].created_at
]

# Every seed column a substituted tie-break could be written against. `id`
# and `created_at` are excluded because they are the keyset itself.
TIE_BREAK_CANDIDATES = (
    "title",
    "description",
    "priority",
    "completed_at",
    "updated_at",
)


async def _walk(service: IssueService, *, first: int) -> list[IssuePage]:
    """Every page, each fetched with the previous page's `endCursor`.

    Bounded rather than `while True`: a cursor that fails to advance -- or a
    `has_next_page` that never goes false -- has to fail this test, not hang
    the suite. One page per seeded row is already far more than any correct
    implementation needs at a page size above one.
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
        f"{len(SEED)} rows. Ids so far: "
        f"{[node.id for page in pages for node in page.nodes]}"
    )


def _ids(page: IssuePage) -> list[UUID]:
    return [node.id for node in page.nodes]


@pytest.fixture
async def seeded(postgres_dsn):
    """A pool onto an `issues` table holding exactly SEED.

    The container is session-scoped, so the schema is dropped first rather
    than assuming this file is the only thing to touch it. Ids and timestamps
    are supplied explicitly: `uuidv7()` and `now()` would both hand back
    values that ascend with insertion order, which is the one thing this
    dataset must not do.
    """
    connection = await asyncpg.connect(postgres_dsn)

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

        # Without this the whole file proves far less than it appears to.
        #
        # A freshly created table has no statistics, and the planner's
        # default guess makes an Index Only Scan over
        # issues_created_at_id_idx look cheapest. That index *is*
        # (created_at DESC, id DESC), so it hands rows back already in the
        # id tie-break order -- even for a query that has stopped asking for
        # one. Deleting `, id DESC` from the repository's ORDER BY therefore
        # changed nothing observable, and every assertion here passed against
        # an implementation with no tie-break at all. ANALYZE gives the
        # planner the real cardinality, it picks Seq Scan + Sort, and the
        # missing sort key becomes visible.
        #
        # This buys realism, not certainty: plan choice is a cost estimate,
        # so a later PostgreSQL, a different random_page_cost or a larger
        # seed could make the index cheap again and quietly re-mask it. The
        # guarantee lives in the forced-plan test in the adversarial sibling
        # file. What ANALYZE buys is that this fixture stops being unlike
        # every real table -- autovacuum analyzes those.
        await connection.execute("ANALYZE issues")
    finally:
        await connection.close()

    pool = await asyncpg.create_pool(dsn=postgres_dsn, min_size=1, max_size=2)

    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
def service(seeded) -> IssueService:
    """The real service over the real repository over the real database."""
    return IssueService(pool=seeded, repository=IssueRepository())


@pytest.fixture
async def pages(service) -> list[IssuePage]:
    return await _walk(service, first=PAGE_SIZE)


def test_the_dataset_splits_a_group_of_tied_timestamps_across_a_page_boundary():
    """The premise the interesting assertions rest on, checked not assumed.

    If a later edit moved the tie so that it began and ended inside one page,
    every other test here would still pass while proving nothing about the id
    tie-break. This is the guard against that.
    """
    assert len(TIED_IDS) > PAGE_SIZE, (
        f"the tie holds {len(TIED_IDS)} rows at page size {PAGE_SIZE}; wider "
        "than a page is what leaves tied rows on both sides of a boundary"
    )
    assert TIED_BOUNDARIES, (
        f"no page boundary falls inside the tie: page size {PAGE_SIZE}, tied "
        f"rows at positions "
        f"{[i for i, row in enumerate(EXPECTED) if row.created_at == TIED]}"
    )

    boundary = TIED_BOUNDARIES[0]
    before, after = EXPECTED[boundary - 1], EXPECTED[boundary]

    # Both sides of the split are inside the tie, so `created_at` cannot
    # decide the boundary and only the id can.
    assert before.id in TIED_IDS
    assert after.id in TIED_IDS
    assert before.created_at == after.created_at
    assert before.id > after.id

    # At least two tied rows survive past the boundary, so the page that
    # resumes there is ordered by the tie-break and not merely selected by
    # the predicate.
    assert len([row for row in EXPECTED[boundary:] if row.id in TIED_IDS]) >= 2

    assert len(EXPECTED_PAGES) == 4
    assert [len(page) for page in EXPECTED_PAGES] == [3, 3, 3, 1]


def test_no_other_seed_column_orders_the_tie_the_way_id_does():
    """The guard on the guard: nothing in the tie stands in for `id`.

    Every ordering assertion in this file is really a claim about the `id`
    tie-break. That claim is only testable if `id` is the *only* thing in the
    seed that produces the expected order inside the tie -- otherwise
    substituting another column into the ORDER BY leaves the whole file
    passing, and the file proves the tie-break by luck rather than by test.
    Three columns were exactly that kind of accidental proxy before this
    test existed, which is why it exists.

    Both directions are checked. Over a set of five a total order is the id
    order, its reverse, or neither, so a column merely inverted to escape
    being a `DESC` proxy becomes an `ASC` one -- which is a real defect this
    file shipped for one revision. Only a scramble is safe.

    Columns that are not a total order over the tie are skipped: they cannot
    act as a deterministic tie-break in the first place. The set of columns
    that survived that skip is then asserted, so that nulling a column out
    cannot quietly turn this check into a no-op.
    """
    tied = sorted(
        (row for row in SEED if row.id in TIED_IDS),
        key=attrgetter("id"),
        reverse=True,
    )
    by_id = tuple(row.id for row in tied)
    reversed_by_id = tuple(reversed(by_id))

    orders: dict[str, tuple[UUID, ...]] = {}

    for column in TIE_BREAK_CANDIDATES:
        key = attrgetter(column)
        values = [key(row) for row in tied]

        if None in values or len(set(values)) < len(values):
            continue

        ordered = tuple(row.id for row in sorted(tied, key=key, reverse=True))
        orders[column] = ordered

        assert ordered != by_id, (
            f"{column} orders the tie exactly as id does, so "
            f"`ORDER BY created_at DESC, {column} DESC` would satisfy every "
            f"assertion in this file"
        )

        # Both directions, because a total order over the tie is the id
        # order, its reverse, or neither -- and only "neither" is safe.
        # Inverting a column to escape the first case lands it in the second.
        assert ordered != reversed_by_id, (
            f"{column} orders the tie as the exact reverse of id, so "
            f"`ORDER BY created_at DESC, {column} ASC` would satisfy every "
            f"assertion in this file"
        )

    assert list(orders) == ["title", "priority", "updated_at"], (
        f"expected title, priority and updated_at to be total orders over "
        f"the tie and so meaningfully checked above; got {list(orders)}"
    )

    # The decoys must not agree with each other either, in either direction.
    # Three columns sharing one permutation is this same bug at smaller
    # scale: it would make two of the checks above restatements of the third.
    seen: dict[tuple[UUID, ...], str] = {}

    for column, ordered in orders.items():
        for form in (ordered, tuple(reversed(ordered))):
            clash = seen.get(form)

            assert clash is None, (
                f"{column} and {clash} order the tie identically up to "
                f"direction, so they are one decoy wearing two names"
            )

        seen[ordered] = column
        seen[tuple(reversed(ordered))] = column


async def test_the_seed_uuids_sort_in_postgres_the_way_python_sorts_them(seeded):
    """Settles the uuid ordering question on the server instead of assuming.

    PostgreSQL compares `uuid` bytewise over the 16-byte value; Python
    compares `UUID.int`. They agree here, but "they agree" is the assumption
    that every explicit expected ordering below is written on top of, so it
    is asked of the real server rather than reasoned about.
    """
    async with seeded.acquire() as connection:
        rows = await connection.fetch("SELECT id FROM issues ORDER BY id DESC")

    assert [row["id"] for row in rows] == sorted(SEED_IDS, reverse=True)


async def test_every_page_is_exactly_the_slice_its_cursor_asked_for(pages):
    """Cursor transitions, page by page -- assertion D.

    Feeding page N's `endCursor` into page N+1 must produce precisely the
    next slice of the expected ordering. Asserting merely that the cursor was
    non-null would pass against a cursor that resumed in the wrong place.
    """
    assert len(pages) == len(EXPECTED_PAGES)

    for index, (page, expected) in enumerate(
        zip(pages, EXPECTED_PAGES, strict=True),
    ):
        assert _ids(page) == [row.id for row in expected], (
            f"page {index + 1} of {len(EXPECTED_PAGES)} is wrong"
        )

    # Every page but the last announced a further one, and did so with a
    # cursor -- which is what made the transitions above possible at all.
    for page in pages[:-1]:
        assert page.has_next_page is True
        assert page.end_cursor is not None


async def test_walking_every_page_reproduces_the_expected_global_ordering(pages):
    """Assertion A: the concatenation of all pages, against one expectation.

    EXPECTED_IDS is `created_at DESC, id DESC` computed from the seed data in
    Python. If the SQL orders any other way -- id ascending inside the tie,
    the wrong timestamp column, no secondary key at all -- this fails.
    """
    walked = [node.id for page in pages for node in page.nodes]

    assert walked == EXPECTED_IDS


async def test_no_row_is_handed_out_twice_and_none_is_skipped(pages):
    """Assertions B and C, which are what a cursor boundary gets wrong.

    An off-by-one in the row-value comparison shows up here as a duplicate
    (`<=`) or a hole (`created_at`-only, dropping the rest of a tie), and
    neither is visible from any single page.
    """
    walked = [node.id for page in pages for node in page.nodes]

    assert len(walked) == len(set(walked)), "a row was returned on two pages"
    assert len(walked) == len(SEED)
    assert set(walked) == SEED_IDS


async def test_a_cursor_inside_a_timestamp_tie_resumes_at_the_next_tied_row(service):
    """Assertion E, the load-bearing one.

    The cursor here points at a row in the middle of the tie. Resuming from
    it must skip exactly the tied rows already handed out and return the next
    tied row -- one whose `created_at` is equal to, not less than, the
    cursor's. `WHERE created_at < $1` returns the same rows for every member
    of the tie and so loses all of them; `(created_at, id) < ($1, $2)` is what
    makes the boundary fall between two rows rather than between two
    timestamps.
    """
    boundary = TIED_BOUNDARIES[0]
    resume_from = EXPECTED[boundary - 1]
    expected = EXPECTED[boundary : boundary + PAGE_SIZE]

    page = await service.list(
        first=PAGE_SIZE,
        after=encode_issue_cursor(resume_from.created_at, resume_from.id),
    )

    assert _ids(page) == [row.id for row in expected]

    # The first row back shares the cursor's timestamp: it survived a
    # comparison that `created_at` alone would have failed.
    assert page.nodes[0].created_at == resume_from.created_at
    assert page.nodes[0].id in TIED_IDS
    assert page.nodes[0].id < resume_from.id

    # More than one row of this page is inside the tie, so the slice
    # assertion above is decided by the id tie-break in the ORDER BY as well
    # as by the resume predicate. With only a single tied row left past the
    # boundary the same assertion would still hold under an `id ASC` sort,
    # and would then be testing the predicate alone.
    tied_in_page = [
        node for node in page.nodes if node.created_at == resume_from.created_at
    ]

    assert len(tied_in_page) >= 2

    # ... and nothing already handed out came back with it.
    assert not {node.id for node in page.nodes} & {
        row.id for row in EXPECTED[:boundary]
    }

    # Every tied row still owed at this boundary comes back on this very
    # page, which is the claim a created_at-only predicate cannot satisfy: it
    # would return the rows below the tie instead and never hand these back.
    # (They fit in one page because the tie leaves fewer than PAGE_SIZE rows
    # past the boundary, which the premise test pins.)
    still_owed = {row.id for row in EXPECTED[boundary:]} & TIED_IDS

    assert still_owed <= {node.id for node in page.nodes}


async def test_the_last_page_is_partial_reports_no_next_page_and_still_has_a_cursor(
    pages,
):
    """Assertion F, against the contract as written rather than as wished.

    `IssueService.list` builds `end_cursor` from the last *returned* node
    whenever there is one, and never from the extra probe row. So a final
    non-empty page carries `has_next_page=False` together with a non-null
    cursor. That is the existing contract and it is pinned here deliberately:
    the cursor marks where the client got to, which is what it needs to
    resume from later, and only `has_next_page` speaks to whether more
    exists.
    """
    final = pages[-1]

    assert len(final.nodes) == len(SEED) % PAGE_SIZE == 1
    assert _ids(final) == [EXPECTED[-1].id]
    assert final.has_next_page is False

    assert final.end_cursor is not None
    assert final.end_cursor == encode_issue_cursor(
        EXPECTED[-1].created_at, EXPECTED[-1].id
    )

    # The cursor is built from the row PostgreSQL returned, so decoding it
    # also shows the microseconds survived the storage and the isoformat
    # round trip intact.
    decoded = decode_issue_cursor(final.end_cursor)

    assert decoded.created_at == EXPECTED[-1].created_at
    assert decoded.id == EXPECTED[-1].id


async def test_paging_past_the_last_row_returns_an_empty_page(pages, service):
    """Assertion G: what one more fetch past the end does today.

    The final page's cursor points at the final row, so asking again is a
    legitimate thing for a polling client to do and it must not error, repeat
    the last row, or claim another page. `end_cursor` comes back None because
    the service builds one only `if nodes` -- meaning a client that keeps
    polling has to hold on to the cursor it already has rather than replace
    it with the None. Pinned as the current behaviour, not adjusted to make a
    tidier assertion.
    """
    page = await service.list(first=PAGE_SIZE, after=pages[-1].end_cursor)

    assert page.nodes == []
    assert page.has_next_page is False
    assert page.end_cursor is None


async def test_every_paged_entity_carries_the_columns_it_was_seeded_with(pages):
    """The SELECT list, checked past the two columns pagination reads.

    Ordering tests would keep passing if `description` or `completed_at`
    dropped out of the query, since neither takes part in the keyset.
    """
    by_id = {row.id: row for row in SEED}

    for page in pages:
        for node in page.nodes:
            seeded_row = by_id[node.id]

            assert node.title == seeded_row.title
            assert node.description == seeded_row.description
            assert node.priority == seeded_row.priority
            assert node.completed_at == seeded_row.completed_at
            assert node.created_at == seeded_row.created_at
            assert node.updated_at == seeded_row.updated_at
