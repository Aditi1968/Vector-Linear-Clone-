"""SearchService against a real PostgreSQL 18.

tests/test_search.py proves what is decided before a statement is issued.
This file is about the statements themselves, and there are four claims worth
a container to make:

  * relevance is what migrations/011_search.sql says it is -- a term in a
    title outranks the same term in a description;
  * `ENG-42` finds ENG-42, by the exact lookup and not by hoping the digits
    survive stemming;
  * a workspace searches its own rows and no others, because `workspace_id`
    is in the WHERE clause and not in a filter over results;
  * the indexes 011 builds are the ones the planner reaches for, with the
    tenant equality evaluated inside the access method rather than above it.

Marked `db`: deselected by default, skipped when Docker is unreachable.
"""

import json
from uuid import UUID

import asyncpg
import pytest

from app.domain.tenancy import WorkspaceScope
from app.repositories.issues import IssueRepository
from app.repositories.projects import ProjectRepository
from app.services.search import SearchService

from tests.conftest import apply_all_migrations, reset_schema, seed_workflow_states


pytestmark = pytest.mark.db

WORKSPACE_A = UUID("00000000-0000-7000-8000-000000000001")
TEAM_A = UUID("00000000-0000-7000-8000-000000000002")
TEAM_A_KEY = "CORE"

WORKSPACE_B = UUID("00000000-0000-7000-8000-0000000000a1")
TEAM_B = UUID("00000000-0000-7000-8000-0000000000a2")
TEAM_B_KEY = "ACME"

SCOPE_A = WorkspaceScope(workspace_id=WORKSPACE_A)
SCOPE_B = WorkspaceScope(workspace_id=WORKSPACE_B)

# The index names 011 declares. Written out rather than derived, because a
# renamed index is exactly the change these EXPLAIN tests exist to notice.
ISSUE_INDEX = "issues_workspace_live_search_idx"
PROJECT_INDEX = "projects_workspace_search_idx"

# A term the bulk dataset below puts on one row in four hundred. Nonsense on
# purpose: a word that occurs in the ordinary seed would make the EXPLAIN
# tests depend on how many rows happened to carry it.
RARE_TERM = "zygomorphic"

# Rows the EXPLAIN fixture adds, split evenly between the two workspaces.
BULK_ROWS = 20000

# The statements under EXPLAIN, kept in the shape the repositories issue.
# They are copies, and deliberately so: EXPLAIN takes SQL text, and a plan
# assertion against a statement built somewhere else would be a plan for a
# query nothing runs.
ISSUE_SEARCH_SQL = """
SELECT id
FROM issues
WHERE workspace_id = $1
    AND archived_at IS NULL
    AND search_vector @@ websearch_to_tsquery('english', $2)
ORDER BY ts_rank(search_vector, websearch_to_tsquery('english', $2)) DESC, id DESC
LIMIT 20
"""

PROJECT_SEARCH_SQL = """
SELECT id
FROM projects
WHERE workspace_id = $1
    AND search_vector @@ websearch_to_tsquery('english', $2)
ORDER BY ts_rank(search_vector, websearch_to_tsquery('english', $2)) DESC, id DESC
LIMIT 20
"""

INSERT_ISSUE_SQL = """
INSERT INTO issues (
    id, workspace_id, team_id, number, workflow_state_id, title, description
)
VALUES (
    $1, $2, $3, $4,
    (
        SELECT id FROM workflow_states
        WHERE workspace_id = $2 AND team_id = $3
        ORDER BY position
        LIMIT 1
    ),
    $5, $6
)
"""

INSERT_PROJECT_SQL = """
INSERT INTO projects (id, workspace_id, name, description, state)
VALUES ($1, $2, $3, $4, 'planned')
"""

# The same insert with the workflow state passed in rather than looked up.
# The bulk fixture below writes twenty thousand rows, and a correlated
# subquery per row makes seeding the slowest part of this file by far.
BULK_INSERT_ISSUE_SQL = """
INSERT INTO issues (
    id, workspace_id, team_id, number, workflow_state_id, title, description
)
VALUES ($1, $2, $3, $4, $5, $6, $7)
"""


def issue_id(index: int) -> UUID:
    return UUID(f"a1b2c3d4-0000-4000-9000-{index:012d}")


def project_id(index: int) -> UUID:
    return UUID(f"b1b2c3d4-0000-4000-9000-{index:012d}")


# The seed, as (index, workspace, team, number, title, description).
#
# Chosen so that "deploy" appears in one issue's title and another's
# description and nowhere else in workspace A, which is what makes the
# ordering assertion a statement about the A/B weighting rather than about
# whatever order the heap happened to hand back.
ISSUE_SEED = (
    (1, WORKSPACE_A, TEAM_A, 1, "Deploy pipeline is flaky", "Retries help a little"),
    (2, WORKSPACE_A, TEAM_A, 2, "Rewrite the changelog", "Blocked on the deploy"),
    (3, WORKSPACE_A, TEAM_A, 3, "Unrelated chore", "Nothing to see"),
    (4, WORKSPACE_A, TEAM_A, 42, "Answer everything", "The identifier test"),
    # Workspace B's copy of the same words. Nothing in A may ever see it.
    (5, WORKSPACE_B, TEAM_B, 1, "Deploy pipeline is flaky", "Someone else's problem"),
    (6, WORKSPACE_B, TEAM_B, 42, "Their forty-two", "Also not A's"),
)

PROJECT_SEED = (
    (1, WORKSPACE_A, "Deploy overhaul", "Ship it"),
    (2, WORKSPACE_A, "Documentation", "Rewrite the deploy guide"),
    (3, WORKSPACE_B, "Deploy overhaul", "Someone else's plan"),
)


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
        TEAM_B_KEY,
    )
    await seed_workflow_states(connection, WORKSPACE_B, TEAM_B)

    for index, workspace, team, number, title, description in ISSUE_SEED:
        await connection.execute(
            INSERT_ISSUE_SQL,
            issue_id(index),
            workspace,
            team,
            number,
            title,
            description,
        )

    for index, workspace, name, description in PROJECT_SEED:
        await connection.execute(
            INSERT_PROJECT_SQL,
            project_id(index),
            workspace,
            name,
            description,
        )


@pytest.fixture
async def pool(postgres_dsn):
    connection = await asyncpg.connect(postgres_dsn)

    try:
        await _seed(connection)
    finally:
        await connection.close()

    created = await asyncpg.create_pool(dsn=postgres_dsn, min_size=1, max_size=2)

    try:
        yield created
    finally:
        await created.close()


@pytest.fixture
def service(pool) -> SearchService:
    """The real service over the real repositories over the real database."""
    return SearchService(
        pool=pool,
        issue_repository=IssueRepository(),
        project_repository=ProjectRepository(),
    )


# ------------------------------------------------------------------ relevance


async def test_a_title_match_outranks_a_description_match(service):
    """The whole of the ranking model 011 declares, in one assertion.

    Issue 1 carries "Deploy" in its title (weight A) and issue 2 carries it in
    its description (weight B). Any ordering that puts them the other way round
    means either the setweight calls were swapped or the ORDER BY is not
    reading the rank at all.
    """
    results = await service.search(scope=SCOPE_A, query="deploy", first=10)

    assert [issue.id for issue in results.issues] == [issue_id(1), issue_id(2)]


async def test_projects_rank_by_name_over_description(service):
    results = await service.search(scope=SCOPE_A, query="deploy", first=10)

    assert [project.id for project in results.projects] == [
        project_id(1),
        project_id(2),
    ]


async def test_the_same_query_returns_the_same_order_every_time(service):
    """A tie must not shuffle between requests.

    `ts_rank` ties constantly, so `id DESC` is what makes the order total.
    Ten runs is not a proof, but an unstable ORDER BY reorders far more often
    than one time in ten.
    """
    orders = set()

    for _ in range(10):
        results = await service.search(scope=SCOPE_A, query="pipeline", first=10)
        orders.add(tuple(issue.id for issue in results.issues))

    assert len(orders) == 1


async def test_stemming_finds_the_inflected_form(service):
    """`english`, not `simple`: "deployed" has to find "Deploy pipeline".

    This is the one behaviour that distinguishes the configuration named in
    the generated column from the default, and it is silent when it breaks --
    no error, just a query that stops matching.
    """
    results = await service.search(scope=SCOPE_A, query="deployed", first=10)

    assert issue_id(1) in {issue.id for issue in results.issues}


# ----------------------------------------------------------------- identifier


async def test_an_identifier_finds_its_issue(service):
    results = await service.search(scope=SCOPE_A, query="CORE-42", first=10)

    assert results.issues[0].id == issue_id(4)
    assert results.issues[0].identifier == f"{TEAM_A_KEY}-42"


async def test_a_lowercase_identifier_finds_the_same_issue(service):
    results = await service.search(scope=SCOPE_A, query="core-42", first=10)

    assert results.issues[0].id == issue_id(4)


async def test_an_identifier_from_another_workspace_finds_nothing(service):
    """`ACME-42` exists. It is not workspace A's, so A must not see it.

    The team is resolved inside the same statement and against the same
    `workspace_id`, so a correctly guessed key from another tenant resolves to
    nothing rather than to that tenant's team.
    """
    results = await service.search(scope=SCOPE_A, query="ACME-42", first=10)

    assert results.issues == []


async def test_an_identifier_naming_no_issue_is_not_an_error(service):
    results = await service.search(scope=SCOPE_A, query="CORE-99999", first=10)

    assert results.issues == []


# -------------------------------------------------------------------- tenancy


async def test_a_search_never_returns_another_workspaces_issue(service):
    """Workspace B holds an issue with an identical title. A cannot see it.

    Two bare words, which `websearch_to_tsquery` ANDs -- so this asks for the
    one issue carrying both, in workspace A. Issue 5 is that issue in
    workspace B, and it is the row a missing tenant predicate would add.
    """
    results = await service.search(scope=SCOPE_A, query="deploy pipeline", first=10)

    assert [issue.id for issue in results.issues] == [issue_id(1)]


async def test_a_search_never_returns_another_workspaces_project(service):
    results = await service.search(scope=SCOPE_A, query="deploy", first=10)

    assert project_id(3) not in {project.id for project in results.projects}


async def test_the_other_workspace_sees_its_own_rows_and_only_those(service):
    """The mirror image, so the isolation is not just A finding nothing."""
    results = await service.search(scope=SCOPE_B, query="deploy", first=10)

    assert [issue.id for issue in results.issues] == [issue_id(5)]
    assert [project.id for project in results.projects] == [project_id(3)]


# ------------------------------------------------------------------- archived


async def test_an_archived_issue_is_not_findable_by_search(service, pool):
    await pool.execute(
        "UPDATE issues SET archived_at = now() WHERE id = $1", issue_id(1)
    )

    results = await service.search(scope=SCOPE_A, query="deploy", first=10)

    assert [issue.id for issue in results.issues] == [issue_id(2)]


async def test_an_archived_issue_is_not_findable_by_identifier(service, pool):
    """The identifier is the one path someone who knows the issue would try."""
    await pool.execute(
        "UPDATE issues SET archived_at = now() WHERE id = $1", issue_id(4)
    )

    results = await service.search(scope=SCOPE_A, query="CORE-42", first=10)

    assert results.issues == []


# ------------------------------------------------------------- garbage in


@pytest.mark.parametrize("query", ["!!!", "??? ...", "-", "&|!", "()"])
async def test_a_punctuation_only_query_returns_nothing_rather_than_everything(
    service, query
):
    """The reason `websearch_to_tsquery` is used and `to_tsquery` is not.

    `to_tsquery('english', '&|!')` raises a syntax error from the server --
    which the schema would mask as an internal error, so a user typing an
    ampersand would see "Internal server error". Here every one of these is a
    tsquery with no terms, which matches no row.
    """
    results = await service.search(scope=SCOPE_A, query=query, first=10)

    assert results.issues == []
    assert results.projects == []


async def test_a_query_matching_nothing_is_empty_and_not_an_error(service):
    results = await service.search(scope=SCOPE_A, query="chlorofluorocarbon", first=10)

    assert results.issues == []
    assert results.projects == []


async def test_the_page_size_bounds_the_results(service, pool):
    await pool.executemany(
        INSERT_ISSUE_SQL,
        [
            (issue_id(100 + n), WORKSPACE_A, TEAM_A, 100 + n, "Deploy again", None)
            for n in range(10)
        ],
    )

    results = await service.search(scope=SCOPE_A, query="deploy", first=3)

    assert len(results.issues) == 3


# --------------------------------------------------------------------- EXPLAIN


def _plan_nodes(node: dict):
    yield node

    for child in node.get("Plans", ()):
        yield from _plan_nodes(child)


async def _plan(connection, sql: str, *args) -> dict:
    rendered = await connection.fetchval(
        f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql}",
        *args,
    )

    return json.loads(rendered)[0]["Plan"]


def _index_scan(plan: dict, index: str) -> dict:
    """The node that reaches rows through `index`, or a failure that says so.

    GIN is only reachable by a bitmap scan, so the node type is
    `Bitmap Index Scan` rather than the `Index Scan` a btree produces.
    """
    scans = [
        node
        for node in _plan_nodes(plan)
        if node.get("Index Name") == index and node["Node Type"] == "Bitmap Index Scan"
    ]

    assert scans, (
        f"nothing in the plan scans {index}; the plan reached rows through "
        f"{[(node['Node Type'], node.get('Index Name')) for node in _plan_nodes(plan)]}"
    )

    return scans[0]


@pytest.fixture
async def planner(postgres_dsn):
    """A dataset big enough that the plan under test is the planner's choice.

    Nothing here is forced. `enable_seqscan = off` would make these tests
    prove only that the index *can* serve the query, and this schema already
    has an index on `(workspace_id, ...)` that can -- a btree that fetches
    every issue in the workspace and rechecks the tsquery above it. Forcing
    would therefore pass with no full-text index at all. The claim worth
    making is the one about cost: at twenty thousand rows, reading a hundred
    through the GIN index beats reading ten thousand through a btree, and the
    planner is the thing that says so.

    Split between two workspaces so that `workspace_id = $1` restricts rather
    than matching everything -- the same reason tests/test_migration_002_db.py
    splits its bulk dataset -- and RARE_TERM lands on one row in a hundred, so
    the term is selective the way a real search term is.

    The cost of that honesty is that these two tests are canaries rather than
    correctness gates: plan selection is an estimate, and a later PostgreSQL
    or a different `random_page_cost` can change it without anything being
    wrong. Every claim about what search *returns* is settled above, without
    a plan.
    """
    connection = await asyncpg.connect(postgres_dsn)

    try:
        await _seed(connection)

        state_a = await connection.fetchval(
            "SELECT id FROM workflow_states WHERE team_id = $1 ORDER BY position",
            TEAM_A,
        )
        state_b = await connection.fetchval(
            "SELECT id FROM workflow_states WHERE team_id = $1 ORDER BY position",
            TEAM_B,
        )

        await connection.executemany(
            BULK_INSERT_ISSUE_SQL,
            [
                (
                    issue_id(1000 + n),
                    WORKSPACE_A if n % 2 == 0 else WORKSPACE_B,
                    TEAM_A if n % 2 == 0 else TEAM_B,
                    1000 + n,
                    state_a if n % 2 == 0 else state_b,
                    f"Routine maintenance {n}",
                    "Nothing about shipping"
                    + (f" {RARE_TERM}" if n % 100 == 0 else ""),
                )
                for n in range(BULK_ROWS)
            ],
        )
        await connection.executemany(
            INSERT_PROJECT_SQL,
            [
                (
                    project_id(1000 + n),
                    WORKSPACE_A if n % 2 == 0 else WORKSPACE_B,
                    f"Initiative {n}",
                    "Routine planning" + (f" {RARE_TERM}" if n % 100 == 0 else ""),
                )
                for n in range(BULK_ROWS)
            ],
        )

        # VACUUM and not just ANALYZE. A GIN index accumulates new entries in
        # a pending list rather than in the tree, and while that list is
        # unflushed every GIN scan is costed as "plus a scan of the whole
        # pending list" -- which makes a freshly bulk-loaded index look
        # expensive and is why an EXPLAIN here without this line reports a
        # plan no production database would choose. VACUUM flushes it, which
        # is what autovacuum does in a running system.
        await connection.execute("VACUUM ANALYZE issues")
        await connection.execute("VACUUM ANALYZE projects")

        yield connection
    finally:
        await connection.close()


async def test_the_issue_search_is_served_by_the_gin_index(planner):
    """The index 011 builds can serve a tenant-scoped search.

    What it catches is the index being absent, misnamed, or built over
    columns this query cannot use -- each of which leaves a sequential scan
    of every issue in the installation on the path of every search, and none
    of which any other test in this file would notice.
    """
    plan = await _plan(planner, ISSUE_SEARCH_SQL, WORKSPACE_A, RARE_TERM)

    _index_scan(plan, ISSUE_INDEX)


async def test_the_tenant_equality_is_evaluated_inside_the_issue_index(planner):
    """What btree_gin buys, asserted rather than assumed.

    Without the extension `workspace_id` cannot be a GIN scan key at all, and
    the tenant equality would have to arrive from a second bitmap or -- worse
    -- from a filter over rows already fetched. Finding both columns in this
    index's own `Index Cond` is what says a workspace's search reads a
    workspace's postings.

    The `Filter` half is the assertion that would catch the bad case: a
    predicate naming workspace_id there means every other tenant's matching
    rows were read from the heap and thrown away.
    """
    plan = await _plan(planner, ISSUE_SEARCH_SQL, WORKSPACE_A, RARE_TERM)

    condition = _index_scan(plan, ISSUE_INDEX)["Index Cond"]

    assert "workspace_id" in condition
    assert "search_vector" in condition

    heap = next(
        node for node in _plan_nodes(plan) if node["Node Type"] == "Bitmap Heap Scan"
    )

    assert "workspace_id" not in heap.get("Filter", "")


async def test_the_project_search_is_served_by_the_gin_index(planner):
    plan = await _plan(planner, PROJECT_SEARCH_SQL, WORKSPACE_A, RARE_TERM)

    assert "workspace_id" in _index_scan(plan, PROJECT_INDEX)["Index Cond"]


async def test_an_archived_issue_is_absent_from_the_index_not_filtered_out(planner):
    """The partial predicate, read off the catalog rather than a plan.

    A plan cannot show this: PostgreSQL omits a partial index's own predicate
    from the qualifications it reports, so an index built without the WHERE
    clause produces an identical-looking plan and simply carries every
    archived issue's postings. The definition is where the difference is
    visible.
    """
    definition = await planner.fetchval(
        "SELECT indexdef FROM pg_indexes WHERE indexname = $1", ISSUE_INDEX
    )

    assert "WHERE (archived_at IS NULL)" in definition
