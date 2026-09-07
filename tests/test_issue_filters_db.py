"""The filter and ordering surface, asked of a real PostgreSQL 18.

Two claims live here, and neither is answerable against a fake connection.

The first is tenancy. Every new filter takes a client-supplied id -- a
project, a cycle, a team, a label, an assignee, a workflow state -- and the
sibling file `test_issue_list_repository.py` proves each one is ANDed onto
`workspace_id = $1` in the statement text. What it cannot prove is that the
conjunction EXCLUDES anything, because a fake connection agrees with whatever
it is handed. So workspace B here holds a real project, a real cycle, real
labels and real issues, and every one of B's ids is aimed at A's list. The
answer must be an empty page: not B's issues, and not A's whole list either --
a filter that silently dropped an unrecognised id would be the same leak
wearing a different shape.

The second is that the keyset walk stays total under every ordering. The
default `(created_at, id)` walk is covered at length by
`test_issue_pagination_db.py` and its adversarial sibling; what is new is
seven more orderings, two of which sort on a key that can be NULL. Those are
where a keyset silently skips or repeats rows, because a row-value comparison
against NULL is NULL rather than false, and because ASC and DESC put the nulls
at opposite ends. Every ordering is therefore walked page by page and compared
against an expectation sorted in Python from the seed literals -- never by
re-running the query under test.

The seed is built so that no two orderings agree. `created_at`, `updated_at`,
`due_date` and `priority` each order the eight rows differently, and each
disagrees with the id order, so a walk that resumed on the wrong column
produces a visibly wrong sequence rather than the right one by luck. Five rows
tie on `priority` and three have no `due_date` at all, so every ordering needs
its tie-break and two need their null branch.

Marked `db`: deselected by default, skipped when Docker is unreachable.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

import asyncpg
import pytest

from app.domain.errors import ValidationError
from app.domain.issues import (
    DEFAULT_ORDER,
    NO_FILTER,
    IssueFilter,
    IssueOrder,
    IssueOrderField,
    OrderDirection,
    order_key,
)
from app.domain.pagination import encode_issue_list_cursor
from app.domain.teams import WorkflowStateCategory
from app.domain.tenancy import WorkspaceScope
from app.repositories.cycles import CycleRepository
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

# Workspace A is the tenant migration 002 seeds, named there as literals so a
# test can assert against a constant. Workspace B is this file's own: isolation
# is not expressible with one tenant.
WORKSPACE_A = UUID("00000000-0000-7000-8000-000000000001")
TEAM_A = UUID("00000000-0000-7000-8000-000000000002")

WORKSPACE_B = UUID("00000000-0000-7000-8000-0000000000b1")
TEAM_B = UUID("00000000-0000-7000-8000-0000000000b2")

SCOPE_A = WorkspaceScope(workspace_id=WORKSPACE_A)
SCOPE_B = WorkspaceScope(workspace_id=WORKSPACE_B)

# A second team inside workspace A, so "another team" and "another workspace"
# are separate questions rather than one.
TEAM_A2 = UUID("00000000-0000-7000-8000-0000000000a2")

USER_ANA = UUID("00000000-0000-7000-8000-0000000000c1")
USER_BEN = UUID("00000000-0000-7000-8000-0000000000c2")
USER_B = UUID("00000000-0000-7000-8000-0000000000c3")

PROJECT_A = UUID("00000000-0000-7000-8000-0000000000d1")
PROJECT_B = UUID("00000000-0000-7000-8000-0000000000d2")

CYCLE_A = UUID("00000000-0000-7000-8000-0000000000e1")
CYCLE_B = UUID("00000000-0000-7000-8000-0000000000e2")

LABEL_A = UUID("00000000-0000-7000-8000-0000000000f1")
LABEL_B = UUID("00000000-0000-7000-8000-0000000000f2")

# argon2id-shaped and never verified against: `users.password_hash` is NOT
# NULL and nothing here authenticates.
FAKE_HASH = "$argon2id$v=19$m=65536,t=3,p=4$c2VlZHNlZWQ$" + "0" * 43

# Small on purpose. At page size three over eight rows the walk makes three
# pages with a boundary inside every tie, and a wrong page is readable at a
# glance.
PAGE_SIZE = 3

BASE = datetime(2026, 4, 1, 9, 0, 0, tzinfo=timezone.utc)


def _issue_id(suffix: int) -> UUID:
    """A UUID whose bytewise order is the decimal order of `suffix`.

    PostgreSQL compares `uuid` bytewise over all sixteen bytes rather than as
    text, so a test may not assume Python's ordering carries over. Holding the
    leading ten bytes fixed and spelling the rest as twelve decimal digits
    makes the two agree by construction.
    """
    return UUID(f"c4d5e6f7-0000-4000-8000-{suffix:012d}")


@dataclass(frozen=True, slots=True)
class Seed:
    suffix: int
    workspace_id: UUID
    team_id: UUID
    created_at: datetime
    updated_at: datetime
    priority: int
    due_date: date | None
    assignee_id: UUID | None = None
    project_id: UUID | None = None
    cycle_id: UUID | None = None
    label_id: UUID | None = None
    category: str = "unstarted"

    @property
    def id(self) -> UUID:
        return _issue_id(self.suffix)


def _at(minutes: int) -> datetime:
    return BASE + timedelta(minutes=minutes)


# Workspace A's eight issues, in an insertion order that is none of the
# orderings under test -- heap order must never be able to stand in for an
# ORDER BY.
#
# Every column is chosen to disagree with every other. `created_at` ascends
# with the suffix; `updated_at` descends against it; `priority` ties five rows
# across two values; `due_date` is missing on three and, where present,
# neither follows nor mirrors the id. So each of the four orderings produces a
# different sequence, and each needs the id tie-break to be total.
SEED_A = (
    Seed(
        suffix=40,
        workspace_id=WORKSPACE_A,
        team_id=TEAM_A,
        created_at=_at(7),
        updated_at=_at(96),
        priority=0,
        due_date=date(2026, 5, 9),
        assignee_id=USER_ANA,
        project_id=PROJECT_A,
        cycle_id=CYCLE_A,
        label_id=LABEL_A,
    ),
    Seed(
        suffix=10,
        workspace_id=WORKSPACE_A,
        team_id=TEAM_A,
        created_at=_at(5),
        updated_at=_at(99),
        priority=2,
        due_date=None,
        assignee_id=USER_ANA,
        category="started",
    ),
    Seed(
        suffix=70,
        workspace_id=WORKSPACE_A,
        team_id=TEAM_A2,
        created_at=_at(6),
        updated_at=_at(93),
        priority=2,
        due_date=date(2026, 5, 2),
        assignee_id=USER_BEN,
        label_id=LABEL_A,
    ),
    Seed(
        suffix=30,
        workspace_id=WORKSPACE_A,
        team_id=TEAM_A,
        created_at=_at(1),
        updated_at=_at(97),
        priority=0,
        due_date=None,
        project_id=PROJECT_A,
        category="completed",
    ),
    Seed(
        suffix=80,
        workspace_id=WORKSPACE_A,
        team_id=TEAM_A2,
        created_at=_at(4),
        updated_at=_at(92),
        priority=4,
        due_date=date(2026, 5, 20),
        assignee_id=USER_BEN,
    ),
    Seed(
        suffix=20,
        workspace_id=WORKSPACE_A,
        team_id=TEAM_A,
        created_at=_at(8),
        updated_at=_at(98),
        priority=1,
        due_date=date(2026, 5, 15),
        project_id=PROJECT_A,
        category="started",
    ),
    Seed(
        suffix=60,
        workspace_id=WORKSPACE_A,
        team_id=TEAM_A,
        created_at=_at(2),
        updated_at=_at(94),
        priority=0,
        due_date=None,
        assignee_id=USER_ANA,
        label_id=LABEL_A,
    ),
    Seed(
        suffix=50,
        workspace_id=WORKSPACE_A,
        team_id=TEAM_A,
        created_at=_at(3),
        updated_at=_at(95),
        priority=2,
        due_date=date(2026, 5, 4),
        cycle_id=CYCLE_A,
    ),
)

# Workspace B's issues exist so that B's ids are REAL. A filter refused
# because the id names nothing proves much less than one refused although the
# id names something -- the second is the leak.
SEED_B = (
    Seed(
        suffix=910,
        workspace_id=WORKSPACE_B,
        team_id=TEAM_B,
        created_at=_at(91),
        updated_at=_at(11),
        priority=1,
        due_date=date(2026, 5, 1),
        assignee_id=USER_B,
        project_id=PROJECT_B,
        cycle_id=CYCLE_B,
        label_id=LABEL_B,
    ),
    Seed(
        suffix=920,
        workspace_id=WORKSPACE_B,
        team_id=TEAM_B,
        created_at=_at(92),
        updated_at=_at(12),
        priority=3,
        due_date=None,
        assignee_id=USER_B,
        project_id=PROJECT_B,
        cycle_id=CYCLE_B,
        label_id=LABEL_B,
    ),
)


INSERT_USER = "INSERT INTO users (id, email, password_hash) VALUES ($1, $2, $3)"

INSERT_MEMBER = """
    INSERT INTO workspace_members (workspace_id, user_id, role)
    VALUES ($1, $2, $3)
"""

INSERT_WORKSPACE = "INSERT INTO workspaces (id, slug, name) VALUES ($1, $2, $3)"

INSERT_TEAM = "INSERT INTO teams (id, workspace_id, name, key) VALUES ($1, $2, $3, $4)"

INSERT_PROJECT = """
    INSERT INTO projects (id, workspace_id, name, state)
    VALUES ($1, $2, $3, 'planned')
"""

INSERT_CYCLE = """
    INSERT INTO cycles (id, workspace_id, team_id, number, name, starts_at, ends_at)
    VALUES ($1, $2, $3, 1, 'Cycle 1', $4, $5)
"""

INSERT_LABEL = """
    INSERT INTO labels (id, workspace_id, name, color)
    VALUES ($1, $2, $3, '#ff0000')
"""

INSERT_ISSUE_LABEL = """
    INSERT INTO issue_labels (workspace_id, issue_id, label_id) VALUES ($1, $2, $3)
"""

# The workflow state is resolved by CATEGORY from the issue's own team, as a
# subquery, so the seed cannot point an issue at another team's board however
# the literals are edited later.
INSERT_ISSUE = """
    INSERT INTO issues (
        id,
        workspace_id,
        team_id,
        number,
        workflow_state_id,
        title,
        priority,
        assignee_id,
        due_date,
        project_id,
        cycle_id,
        created_at,
        updated_at
    )
    VALUES (
        $1, $2, $3, $4,
        (
            SELECT id
            FROM workflow_states
            WHERE workspace_id = $2 AND team_id = $3 AND type = $5
            ORDER BY position, id
            LIMIT 1
        ),
        $6, $7, $8, $9, $10, $11, $12, $13
    )
"""


@dataclass(frozen=True, slots=True)
class Wired:
    service: IssueService
    cycles: CycleRepository
    pool: asyncpg.Pool


@pytest.fixture
async def wired(postgres_dsn):
    """The whole migration chain, two tenants populated, a real pool."""
    connection = await asyncpg.connect(postgres_dsn)

    try:
        await reset_schema(connection)
        await apply_all_migrations(connection)

        await connection.execute(INSERT_WORKSPACE, WORKSPACE_B, "beta", "Beta")
        await connection.execute(INSERT_TEAM, TEAM_B, WORKSPACE_B, "Beta Core", "BETA")
        await connection.execute(INSERT_TEAM, TEAM_A2, WORKSPACE_A, "Design", "DES")

        # 005 seeded boards for the teams that existed when it ran; both of
        # these were created afterwards.
        await seed_workflow_states(connection, WORKSPACE_B, TEAM_B)
        await seed_workflow_states(connection, WORKSPACE_A, TEAM_A2)

        for user, email in (
            (USER_ANA, "ana@example.test"),
            (USER_BEN, "ben@example.test"),
            (USER_B, "bee@example.test"),
        ):
            await connection.execute(INSERT_USER, user, email, FAKE_HASH)

        # `issues_assignee_fk` is composite over (workspace_id, assignee_id)
        # against `workspace_members`, so an assignee has to be a member of
        # the workspace its issue is in -- which is what makes an assignee
        # filter a tenant-scoped one for free.
        for workspace, user in (
            (WORKSPACE_A, USER_ANA),
            (WORKSPACE_A, USER_BEN),
            (WORKSPACE_B, USER_B),
        ):
            await connection.execute(INSERT_MEMBER, workspace, user, "member")

        for project, workspace, name in (
            (PROJECT_A, WORKSPACE_A, "Alpha plan"),
            (PROJECT_B, WORKSPACE_B, "Beta plan"),
        ):
            await connection.execute(INSERT_PROJECT, project, workspace, name)

        for cycle, workspace, team in (
            (CYCLE_A, WORKSPACE_A, TEAM_A),
            (CYCLE_B, WORKSPACE_B, TEAM_B),
        ):
            await connection.execute(
                INSERT_CYCLE, cycle, workspace, team, _at(0), _at(20160)
            )

        for label, workspace, name in (
            (LABEL_A, WORKSPACE_A, "bug"),
            (LABEL_B, WORKSPACE_B, "bug"),
        ):
            await connection.execute(INSERT_LABEL, label, workspace, name)

        numbers: dict[UUID, int] = {}

        for row in (*SEED_A, *SEED_B):
            numbers[row.team_id] = numbers.get(row.team_id, 0) + 1

            await connection.execute(
                INSERT_ISSUE,
                row.id,
                row.workspace_id,
                row.team_id,
                numbers[row.team_id],
                row.category,
                f"issue {row.suffix}",
                row.priority,
                row.assignee_id,
                row.due_date,
                row.project_id,
                row.cycle_id,
                row.created_at,
                row.updated_at,
            )

            if row.label_id is not None:
                await connection.execute(
                    INSERT_ISSUE_LABEL, row.workspace_id, row.id, row.label_id
                )

        # Real statistics, so the planner chooses among the indexes 015 adds
        # rather than seq-scanning a table it has never met. A filter that is
        # only correct on an unanalysed eight-row table is not correct.
        await connection.execute("ANALYZE")
    finally:
        await connection.close()

    pool = await asyncpg.create_pool(dsn=postgres_dsn, min_size=1, max_size=3)

    try:
        yield Wired(
            service=IssueService(
                pool=pool,
                repository=IssueRepository(),
                teams=TeamService(pool=pool, repository=TeamRepository()),
            ),
            cycles=CycleRepository(),
            pool=pool,
        )
    finally:
        await pool.close()


async def _walk(
    service: IssueService,
    *,
    scope: WorkspaceScope = SCOPE_A,
    issue_filter: IssueFilter = NO_FILTER,
    order: IssueOrder = DEFAULT_ORDER,
    first: int = PAGE_SIZE,
) -> list[UUID]:
    """Every id the walk returns, page by page, following endCursor.

    Bounded, so a cursor that fails to advance fails the test rather than
    hanging the suite. Duplicates are NOT collapsed: a keyset that repeats a
    row is exactly the bug this file is looking for, so the caller compares
    against a list and not a set.
    """
    ids: list[UUID] = []
    after: str | None = None

    for _ in range(len(SEED_A) + len(SEED_B) + 2):
        page = await service.list(
            scope=scope,
            first=first,
            after=after,
            issue_filter=issue_filter,
            order=order,
        )

        ids.extend(node.id for node in page.nodes)

        if not page.has_next_page:
            return ids

        after = page.end_cursor

    raise AssertionError(f"pagination did not terminate after {len(ids)} rows")


def _expected(order: IssueOrder, rows=SEED_A) -> list[UUID]:
    """The ids in the order the server is required to return them.

    Sorted in Python from the seed literals, restating PostgreSQL's own rules
    rather than asking the code under test what the answer is:

    * the key, then the id, both in the same direction -- which is what makes
      the ordering total;
    * NULLs last ascending and first descending, which are PostgreSQL's
      defaults and the reason the repository writes no NULLS clause;
    * priority 0 is not a priority, so its key is NULL.
    """
    # The sort key is (is_null, value, id): the null flag first, so the nulls
    # sort as one block, and `reverse=True` flips it -- which is exactly what
    # makes ASC put them last and DESC put them first.
    descending = order.direction is OrderDirection.DESC

    return [
        row.id
        for row in sorted(
            rows,
            key=lambda row: _sort_tuple(row, order.field),
            reverse=descending,
        )
    ]


def _key_of(row: Seed, field: IssueOrderField):
    if field is IssueOrderField.PRIORITY:
        return row.priority or None

    if field is IssueOrderField.CREATED_AT:
        return row.created_at

    if field is IssueOrderField.UPDATED_AT:
        return row.updated_at

    return row.due_date


def _comparable(value):
    """A stand-in the sort can compare when the real key is NULL.

    Never compared against a real value -- the `is None` flag separates the
    two groups first -- so the constant only has to be of a type that
    compares with itself.
    """
    return 0 if value is None else value


def _sort_tuple(row: Seed, field: IssueOrderField):
    value = _key_of(row, field)

    return (value is None, _comparable(value), row.id)


def test_no_two_orderings_agree_on_the_seed():
    """The premise every ordering assertion below rests on.

    If an edit made two of these produce the same sequence, a walk that
    resumed on the wrong column would still look right, and half this file
    would quietly stop testing anything.
    """
    sequences = {
        field: tuple(_expected(IssueOrder(field=field))) for field in IssueOrderField
    }

    assert len(set(sequences.values())) == len(sequences)

    by_id = tuple(sorted((row.id for row in SEED_A), reverse=True))

    for field, sequence in sequences.items():
        assert sequence != by_id, f"{field} must not agree with the id order"


@pytest.mark.parametrize("field", list(IssueOrderField))
@pytest.mark.parametrize("direction", list(OrderDirection))
async def test_a_full_walk_returns_every_row_exactly_once_in_order(
    wired, field, direction
):
    """Eight orderings, each walked in pages of three.

    This is the assertion the whole ordering feature stands on. A keyset walk
    over a non-total order does not fail: it returns a page. Rows either side
    of a tie get skipped or served twice, and the only way to see it is to
    compare the concatenated walk against an independently computed sequence
    -- length included, because a skip and a repeat both leave a plausible
    page behind.
    """
    order = IssueOrder(field=field, direction=direction)

    walked = await _walk(wired.service, order=order)

    assert walked == _expected(order)
    assert len(walked) == len(SEED_A)


@pytest.mark.parametrize("field", list(IssueOrderField))
@pytest.mark.parametrize("direction", list(OrderDirection))
async def test_a_page_size_of_one_walks_every_boundary(wired, field, direction):
    """The same walk with a boundary between every pair of adjacent rows.

    Page size three lands the boundary in some ties and not others; page size
    one puts it everywhere, including on both sides of a NULL key, which is
    where the four-way branch in `_add_keyset` is either right or silently
    wrong.
    """
    order = IssueOrder(field=field, direction=direction)

    assert await _walk(wired.service, order=order, first=1) == _expected(order)


async def test_a_walk_under_a_filter_is_still_total(wired):
    """Filters and the keyset compose: the walk narrows without losing rows."""
    order = IssueOrder(field=IssueOrderField.DUE_DATE, direction=OrderDirection.ASC)
    issue_filter = IssueFilter(assignee_id=USER_ANA)

    walked = await _walk(wired.service, issue_filter=issue_filter, order=order, first=1)
    expected = _expected(
        order, rows=tuple(r for r in SEED_A if r.assignee_id == USER_ANA)
    )

    assert walked == expected
    assert len(walked) == 3


# Every id-shaped filter, the workspace-B value aimed at workspace A, and how
# many of A's issues the same filter selects when given A's own id. The last
# number is what makes each row a real test rather than two ways of asking for
# nothing.
FOREIGN_IDS = [
    ("team_id", TEAM_B, None),
    ("assignee_id", USER_B, None),
    ("project_id", PROJECT_B, PROJECT_A),
    ("cycle_id", CYCLE_B, CYCLE_A),
    ("label_id", LABEL_B, LABEL_A),
]


@pytest.mark.parametrize(("field", "foreign", "own"), FOREIGN_IDS)
async def test_a_filter_id_from_another_workspace_selects_nothing(
    wired, field, foreign, own
):
    """The id is real, it names rows, and none of them may be reachable here.

    A filter that widened -- selecting on the id alone, or dropping an
    unrecognised one -- would return B's issues or A's whole list. Both are
    checked, because "empty" is only meaningful next to what the same query
    returns for a value that does belong here.
    """
    page = await wired.service.list(
        scope=SCOPE_A,
        first=50,
        after=None,
        issue_filter=IssueFilter(**{field: foreign}),
    )

    assert page.nodes == []
    assert page.has_next_page is False

    unfiltered = await wired.service.list(scope=SCOPE_A, first=50, after=None)

    assert len(unfiltered.nodes) == len(SEED_A)

    if own is not None:
        matching = await wired.service.list(
            scope=SCOPE_A,
            first=50,
            after=None,
            issue_filter=IssueFilter(**{field: own}),
        )

        assert matching.nodes != []


@pytest.mark.parametrize(("field", "foreign", "_own"), FOREIGN_IDS)
async def test_the_same_id_does_select_rows_in_its_own_workspace(
    wired, field, foreign, _own
):
    """The other half: B's ids are not inert, they are simply not A's.

    Without this, every assertion above would pass against a filter that
    matched nothing anywhere.
    """
    page = await wired.service.list(
        scope=SCOPE_B,
        first=50,
        after=None,
        issue_filter=IssueFilter(**{field: foreign}),
    )

    assert [node.id for node in page.nodes] != []


async def test_a_foreign_workflow_state_selects_nothing(wired):
    """A state id is a client value too, and it names a row in another tenant."""
    state_id = await wired.pool.fetchval(
        """
        SELECT id
        FROM workflow_states
        WHERE workspace_id = $1 AND team_id = $2 AND type = 'unstarted'
        ORDER BY position, id
        LIMIT 1
        """,
        WORKSPACE_B,
        TEAM_B,
    )

    page = await wired.service.list(
        scope=SCOPE_A,
        first=50,
        after=None,
        issue_filter=IssueFilter(workflow_state_id=state_id),
    )

    assert page.nodes == []

    in_b = await wired.service.list(
        scope=SCOPE_B,
        first=50,
        after=None,
        issue_filter=IssueFilter(workflow_state_id=state_id),
    )

    assert in_b.nodes != []


async def test_a_cursor_minted_in_another_workspace_resumes_nothing_here(wired):
    """The cursor carries an ordering and a position, never a tenant.

    It cannot carry one: it is Base64 over JSON that anybody holding it can
    read and rewrite. So the workspace comes from the request instead, and
    replaying B's cursor against A resumes at that POSITION inside A -- B's
    rows are all newer, so the resume point is ahead of everything A has and
    A's whole list comes back. What must not come back, under any reading of
    that cursor, is a row of B's.
    """
    b_page = await wired.service.list(scope=SCOPE_B, first=1, after=None)

    resumed = await wired.service.list(
        scope=SCOPE_A,
        first=50,
        after=b_page.end_cursor,
    )

    returned = {node.id for node in resumed.nodes}

    assert returned == {row.id for row in SEED_A}
    assert returned.isdisjoint({row.id for row in SEED_B})


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("assignee_id", (30, 20, 50)),
        ("project_id", (70, 80, 60, 10, 50)),
        ("cycle_id", (70, 30, 20, 60, 10, 80)),
    ],
)
async def test_an_explicit_null_filter_selects_the_rows_holding_nothing(
    wired, field, expected
):
    """`assigneeId: null` is the unassigned queue, not the absence of a filter.

    `= NULL` is never true, so a filter that bound None as a parameter would
    return an empty page here -- with a perfectly valid statement and no
    error anywhere.
    """
    page = await wired.service.list(
        scope=SCOPE_A,
        first=50,
        after=None,
        issue_filter=IssueFilter(**{field: None}),
    )

    assert {node.id for node in page.nodes} == {_issue_id(s) for s in expected}


async def test_the_state_category_filter_reads_the_teams_own_board(wired):
    """A category spans teams, and the subquery that resolves it is scoped.

    Both A's teams have their own five states, so 'started' names two
    different state ids -- which is the case a filter on `workflowStateId`
    cannot express and this one has to.
    """
    page = await wired.service.list(
        scope=SCOPE_A,
        first=50,
        after=None,
        issue_filter=IssueFilter(state_category=WorkflowStateCategory.STARTED),
    )

    assert {node.id for node in page.nodes} == {_issue_id(10), _issue_id(20)}

    completed = await wired.service.list(
        scope=SCOPE_A,
        first=50,
        after=None,
        issue_filter=IssueFilter(state_category=WorkflowStateCategory.COMPLETED),
    )

    assert {node.id for node in completed.nodes} == {_issue_id(30)}


async def test_filters_compose_as_a_conjunction(wired):
    """Two filters narrow together; neither replaces the other."""
    page = await wired.service.list(
        scope=SCOPE_A,
        first=50,
        after=None,
        issue_filter=IssueFilter(assignee_id=USER_ANA, label_id=LABEL_A),
    )

    assert {node.id for node in page.nodes} == {_issue_id(40), _issue_id(60)}


async def test_total_count_is_the_whole_match_and_not_the_page(wired):
    """The number a column header states, against a page that holds three."""
    page = await wired.service.list(scope=SCOPE_A, first=PAGE_SIZE, after=None)

    assert len(page.nodes) == PAGE_SIZE
    assert await wired.service.count(scope=SCOPE_A) == len(SEED_A)


async def test_total_count_answers_the_same_question_as_the_page(wired):
    """Same filter, same predicate, same answer -- counted and walked."""
    issue_filter = IssueFilter(team_id=TEAM_A, priority=0)

    walked = await _walk(wired.service, issue_filter=issue_filter, first=1)
    counted = await wired.service.count(scope=SCOPE_A, issue_filter=issue_filter)

    assert counted == len(walked)
    assert counted == 3


async def test_total_count_stays_inside_the_workspace(wired):
    """An aggregate is the easiest read to leave unscoped: nothing renders wrong."""
    assert await wired.service.count(scope=SCOPE_B) == len(SEED_B)

    foreign = await wired.service.count(
        scope=SCOPE_A,
        issue_filter=IssueFilter(project_id=PROJECT_B),
    )

    assert foreign == 0


async def test_an_archived_issue_leaves_both_the_page_and_the_count(wired):
    """`archived_at IS NULL` is in the count for the same reason it is in the page.

    A number that included archived issues would make every board column
    claim more work than it can show.
    """
    await wired.pool.execute(
        "UPDATE issues SET archived_at = now() WHERE id = $1",
        _issue_id(40),
    )

    assert await wired.service.count(scope=SCOPE_A) == len(SEED_A) - 1
    assert _issue_id(40) not in await _walk(wired.service)


async def test_a_cursor_from_another_ordering_is_refused_by_the_service(wired):
    """The one input error the ordering surface adds, end to end."""
    page = await wired.service.list(scope=SCOPE_A, first=1, after=None)

    with pytest.raises(ValidationError) as raised:
        await wired.service.list(
            scope=SCOPE_A,
            first=1,
            after=page.end_cursor,
            order=IssueOrder(field=IssueOrderField.DUE_DATE),
        )

    assert [issue.code for issue in raised.value.issues] == ["ORDER_MISMATCH"]


async def test_the_end_cursor_matches_the_key_the_server_sorted_by(wired):
    """The cursor is minted from the row the server returned, not from the seed.

    `order_key` is the domain's reading of a row and the repository's SQL is
    the server's; this is where the two are shown to agree on a real row,
    including the untriaged ones whose key is NULL.
    """
    order = IssueOrder(field=IssueOrderField.PRIORITY, direction=OrderDirection.ASC)

    page = await wired.service.list(scope=SCOPE_A, first=5, after=None, order=order)
    last = page.nodes[-1]

    assert page.end_cursor == encode_issue_list_cursor(
        order, order_key(last, order.field), last.id
    )


async def test_a_cycle_carries_the_team_it_belongs_to(wired):
    """`Cycle.teamId`, from the column migration 008 already had.

    Its absence is what forced a cycle screen to page the whole workspace: a
    client holding only a cycle had no team to scope its issue list to, and
    the index that makes a cycle filter cheap is keyed on the team.
    """
    async with wired.pool.acquire() as connection:
        cycle = await wired.cycles.get_by_id(
            connection, scope=SCOPE_A, cycle_id=CYCLE_A
        )

        listed = await wired.cycles.list_for_team(
            connection, scope=SCOPE_A, team_id=TEAM_A
        )

        foreign = await wired.cycles.get_by_id(
            connection, scope=SCOPE_A, cycle_id=CYCLE_B
        )

    assert cycle is not None
    assert cycle.team_id == TEAM_A
    assert [entity.team_id for entity in listed] == [TEAM_A]

    # The new field changes nothing about isolation: another tenant's cycle is
    # still the same answer as one that does not exist.
    assert foreign is None


async def test_the_cycle_filter_scoped_to_its_own_team_is_the_shape_015_indexes(
    wired,
):
    """A cycle filter sent with its team, which is what `Cycle.teamId` enables.

    008's issues_workspace_team_cycle_idx is keyed (workspace_id, team_id,
    cycle_id), so a cycle filter is cheap exactly when the team travels with
    it. This asserts the two compose to the same answer the cycle alone gives
    -- a cycle belongs to one team, so adding its team must never narrow the
    result.
    """
    by_cycle = await wired.service.list(
        scope=SCOPE_A,
        first=50,
        after=None,
        issue_filter=IssueFilter(cycle_id=CYCLE_A),
    )

    with_team = await wired.service.list(
        scope=SCOPE_A,
        first=50,
        after=None,
        issue_filter=IssueFilter(cycle_id=CYCLE_A, team_id=TEAM_A),
    )

    # Identical, and that is the claim: `issues_cycle_fk` is composite over
    # (workspace_id, team_id, cycle_id), so every issue in a cycle is already
    # of that cycle's team. Sending the team costs the client nothing and buys
    # the query an index it otherwise cannot use.
    assert {node.id for node in by_cycle.nodes} == {_issue_id(40), _issue_id(50)}
    assert {node.id for node in with_team.nodes} == {node.id for node in by_cycle.nodes}


# The indexes 015 adds, by name. Asserting they EXIST rather than that a plan
# uses them: a plan is the planner's business and changes with statistics and
# with the version, but an index that failed to be created is a silent
# regression to a sort of the whole workspace.
INDEXES_015 = (
    "issues_workspace_live_assignee_created_at_id_idx",
    "issues_workspace_live_updated_at_id_idx",
    "issues_workspace_live_priority_id_idx",
    "issues_workspace_live_due_date_id_idx",
)


@pytest.mark.parametrize("name", INDEXES_015)
async def test_migration_015_created_its_index(wired, name):
    assert (
        await wired.pool.fetchval(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'issues' AND indexname = $1",
            name,
        )
        == name
    )


@pytest.mark.parametrize("field", list(IssueOrderField))
@pytest.mark.parametrize("direction", list(OrderDirection))
async def test_every_ordering_can_be_served_by_an_index(wired, field, direction):
    """One index per key, read forwards or backwards -- never a sort.

    The claim 015 makes is that a btree scanned in either direction covers
    both directions of an ordering, which holds only because the id tie-break
    carries the same direction as the key and because the query writes no
    NULLS clause. If either stopped being true, the planner would fall back to
    sorting the workspace, and the only visible symptom would be a slow page.

    Sequential scans are disabled to ask the question of the planner:
    over eight rows it would otherwise choose a scan and a sort every time,
    whatever indexes exist.
    """
    key_sql = {
        IssueOrderField.PRIORITY: "NULLIF(issues.priority, 0)",
        IssueOrderField.CREATED_AT: "issues.created_at",
        IssueOrderField.UPDATED_AT: "issues.updated_at",
        IssueOrderField.DUE_DATE: "issues.due_date",
    }[field]
    word = "DESC" if direction is OrderDirection.DESC else "ASC"

    async with wired.pool.acquire() as connection, connection.transaction():
        # SET LOCAL needs a transaction to be local to; without one it is a
        # no-op with a warning, and the assertion below would be measuring
        # the planner's ordinary preference on an eight-row table.
        await connection.execute("SET LOCAL enable_seqscan = off")
        await connection.execute("SET LOCAL enable_sort = off")

        rows = await connection.fetch(
            f"""
            EXPLAIN (FORMAT TEXT)
            SELECT issues.id
            FROM issues
            WHERE issues.workspace_id = $1 AND issues.archived_at IS NULL
            ORDER BY {key_sql} {word}, issues.id {word}
            LIMIT 3
            """,
            WORKSPACE_A,
        )

    plan = chr(10).join(row[0] for row in rows)

    assert "Index Scan" in plan or "Index Only Scan" in plan, plan
    assert "Sort" not in plan, plan


async def test_the_batch_lookup_cannot_reach_across_the_tenant_boundary(wired):
    """`Notification.issue` resolves ids that arrived on a row, not from a client.

    That makes it the read most likely to be trusted: the server wrote the id,
    so it looks safe to look up without a workspace. It is not -- a mismatched
    row, or a loader keyed on the id alone, would surface another tenant's
    issue -- so the batch carries the scope and B's ids answer nothing in A.
    """
    both = [_issue_id(40), _issue_id(910)]

    in_a = await wired.service.get_many_by_ids(scope=SCOPE_A, issue_ids=both)
    in_b = await wired.service.get_many_by_ids(scope=SCOPE_B, issue_ids=both)

    assert [entity.id for entity in in_a] == [_issue_id(40)]
    assert [entity.id for entity in in_b] == [_issue_id(910)]


async def test_the_batch_lookup_omits_an_archived_issue(wired):
    """The same null an archived issue gives on every other read."""
    await wired.pool.execute(
        "UPDATE issues SET archived_at = now() WHERE id = $1",
        _issue_id(40),
    )

    found = await wired.service.get_many_by_ids(
        scope=SCOPE_A,
        issue_ids=[_issue_id(40), _issue_id(50)],
    )

    assert [entity.id for entity in found] == [_issue_id(50)]
