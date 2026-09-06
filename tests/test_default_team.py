"""Choosing the team an issue is filed against, at three levels.

`issues.team_id` is NOT NULL and has no default, so every insert names a
team. The product cannot name one yet -- no GraphQL argument, no team
concept in the UI -- so something has to choose, and the choice is the kind
that fails quietly when it is wrong: a team id is a UUID like any other, and
against a bootstrap tenant holding exactly one team, almost any wrong answer
still inserts successfully.

Three levels, each catching a failure the others cannot see:

  * Section A reads the statement the repository sends. A lookup missing its
    workspace predicate returns another tenant's team, and one missing its
    ORDER BY returns an arbitrary team that can differ per call and per
    replica -- neither is visible in the value that comes back.
  * Section B pins what the service does with a miss. A workspace with no
    teams must raise; returning None would push an unavoidable failure into
    the caller's insert, and falling back to any team at all would be a
    cross-tenant write.
  * Section C runs the real mutation against a real PostgreSQL. This is the
    one that answers the question the phase exists for: 002 made
    `issueCreate` fail with a NOT NULL violation, and only a server can say
    that it now succeeds and that the row lands in the right tenant.

Section C is marked `db`: deselected by default, skipped when Docker is
unreachable. Nothing here touches DATABASE_URL or Neon.
"""

from pathlib import Path
from uuid import UUID

import asyncpg
import pytest

from app.domain.errors import TeamNotFoundError
from app.graphql.context import VectorContext
from app.graphql.schema import build_schema
from app.graphql.tenancy import RequestTenant
from app.repositories.issues import IssueRepository
from app.repositories.teams import TeamRepository
from app.repositories.workspaces import WorkspaceRepository
from app.services.issues import IssueService
from app.services.teams import TeamService
from app.services.workspaces import WorkspaceService
from scripts.apply_migration import apply_migration, read_migration

from tests.conftest import (
    TEST_SCOPE,
    TEST_WORKSPACE_ID,
    FakeConnection,
    FakePool,
    normalize,
)


MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"
MIGRATION_001 = MIGRATIONS_DIR / "001_issues.sql"
MIGRATION_002 = MIGRATIONS_DIR / "002_tenancy.sql"

# The tenant 002 seeds, written as literals rather than read back out of the
# database: 002 names both rows in the file precisely so that a test can
# assert against a constant instead of querying for the value it is about to
# check.
BOOTSTRAP_WORKSPACE_ID = UUID("00000000-0000-7000-8000-000000000001")
BOOTSTRAP_TEAM_ID = UUID("00000000-0000-7000-8000-000000000002")

# A second workspace with a team of its own, created by section C rather than
# by the migration. Without it, "resolves the bootstrap team" is also
# satisfied by an implementation that returns the only team in the table.
OTHER_WORKSPACE_ID = UUID("00000000-0000-7000-8000-0000000000d1")
OTHER_TEAM_ID = UUID("00000000-0000-7000-8000-0000000000d2")

# A team inserted into the bootstrap workspace *after* the bootstrap team, so
# that "oldest" is a claim with two candidates rather than one.
LATER_TEAM_ID = UUID("00000000-0000-7000-8000-0000000000d3")

ISSUE_CREATE_MUTATION = """
mutation CreateIssue($input: IssueCreateInput!) {
  issueCreate(input: $input) {
    issue {
      id
      title
      priority
    }

    errors {
      field
      code
      message
    }
  }
}
"""


# --------------------------------------------------------------------------
# A. The statement the repository sends
# --------------------------------------------------------------------------


async def test_the_team_lookup_is_scoped_ordered_and_bounded():
    """All three clauses, because each one alone is silently insufficient.

    Without `WHERE workspace_id = $1` the query answers with whatever team
    happens to sort first across every tenant in the database. Without the
    ORDER BY it answers with whatever the server hands back first, which is
    not stable across calls, vacuums or replicas -- so an issue's team would
    depend on physical row order. Without the LIMIT it reads the workspace's
    whole team list to use one row of it.
    """
    connection = FakeConnection(row=None)

    await TeamRepository().find_oldest_id(connection, scope=TEST_SCOPE)

    query = normalize(connection.queries[0]["query"])

    assert "WHERE workspace_id = $1" in query
    assert "ORDER BY created_at, id" in query
    assert "LIMIT 1" in query

    # Bound as a parameter, never interpolated into the SQL.
    assert connection.queries[0]["args"] == (TEST_WORKSPACE_ID,)
    assert str(TEST_WORKSPACE_ID) not in query


async def test_the_team_lookup_returns_the_id_it_was_given():
    connection = FakeConnection(row={"id": BOOTSTRAP_TEAM_ID})

    found = await TeamRepository().find_oldest_id(connection, scope=TEST_SCOPE)

    assert found == BOOTSTRAP_TEAM_ID


async def test_a_workspace_with_no_teams_is_an_ordinary_empty_answer():
    """The repository reports the absence; deciding what it means is the service's."""
    connection = FakeConnection(row=None)

    assert await TeamRepository().find_oldest_id(connection, scope=TEST_SCOPE) is None


# --------------------------------------------------------------------------
# B. What the service does with a miss
# --------------------------------------------------------------------------


async def test_the_service_raises_rather_than_returning_no_team():
    """The failure mode this layer exists to refuse.

    Two wrong answers are available here and both look harmless. Returning
    None moves the failure into the caller's INSERT, where it surfaces as a
    NOT NULL violation with nothing to say which workspace was
    unprovisioned. Falling back to any team the database holds is worse: it
    would file the issue in another tenant, successfully, with no error
    anywhere.
    """
    pool = FakePool(FakeConnection(row=None))
    service = TeamService(pool=pool, repository=TeamRepository())

    with pytest.raises(TeamNotFoundError) as error:
        await service.default_team_id(TEST_SCOPE)

    assert error.value.args == ("Team not found",)
    assert pool.acquire_count == 1


async def test_the_service_returns_the_team_the_repository_found():
    pool = FakePool(FakeConnection(row={"id": BOOTSTRAP_TEAM_ID}))
    service = TeamService(pool=pool, repository=TeamRepository())

    assert await service.default_team_id(TEST_SCOPE) == BOOTSTRAP_TEAM_ID


# --------------------------------------------------------------------------
# C. The real mutation against a real server
# --------------------------------------------------------------------------


db = pytest.mark.db

# Built directly rather than imported, so these tests need no DATABASE_URL.
schema = build_schema("test")


@pytest.fixture
async def wired(postgres_dsn):
    """001 then 002 through the runner, three teams, the real context.

    The schema is built the way tests/test_workspace_resolution.py's
    `resolved` fixture builds it: hand-written DDL would be a second
    definition of the schema, and the claim under test is about the one the
    migrations produce.

    Two extra teams are inserted, and both are load-bearing. The one in
    another workspace makes "scoped to the caller's tenant" falsifiable. The
    later one inside the bootstrap workspace makes "oldest" falsifiable --
    with a single team, every selection rule agrees.

    The context is the product's own `VectorContext` holding the product's
    own `RequestTenant`, not a fake. That is the point of this section: the
    fakes elsewhere prove the resolvers forward what they are handed, and
    only this proves the thing that hands it to them works against a
    database.
    """
    connection = await asyncpg.connect(postgres_dsn)

    try:
        await connection.execute("DROP TABLE IF EXISTS issues, teams, workspaces")
        await connection.execute("DROP TABLE IF EXISTS schema_migrations")
        await connection.execute(read_migration(MIGRATION_001))

        async with connection.transaction():
            await apply_migration(
                connection,
                MIGRATION_002,
                migrations_dir=MIGRATIONS_DIR,
            )

        await connection.execute(
            "INSERT INTO workspaces (id, slug, name) VALUES ($1, $2, $3)",
            OTHER_WORKSPACE_ID,
            "acme",
            "Acme",
        )

        # Timestamps are explicit so that "oldest" is decided by the data and
        # not by how fast three inserts ran. The other workspace's team is
        # the oldest row in the whole table, so a lookup that dropped its
        # workspace predicate returns it.
        await connection.execute(
            """
            INSERT INTO teams (id, workspace_id, name, created_at)
            VALUES ($1, $2, $3, TIMESTAMPTZ '2020-01-01 00:00:00+00')
            """,
            OTHER_TEAM_ID,
            OTHER_WORKSPACE_ID,
            "Acme Core",
        )
        await connection.execute(
            """
            INSERT INTO teams (id, workspace_id, name, created_at)
            VALUES ($1, $2, $3, TIMESTAMPTZ '2030-01-01 00:00:00+00')
            """,
            LATER_TEAM_ID,
            BOOTSTRAP_WORKSPACE_ID,
            "Platform",
        )

        pool = await asyncpg.create_pool(dsn=postgres_dsn, min_size=1, max_size=2)

        try:
            yield (
                VectorContext(
                    issue_service=IssueService(pool=pool, repository=IssueRepository()),
                    tenant=RequestTenant(
                        workspace_service=WorkspaceService(
                            pool=pool,
                            repository=WorkspaceRepository(),
                        ),
                        team_service=TeamService(
                            pool=pool, repository=TeamRepository()
                        ),
                    ),
                ),
                connection,
            )
        finally:
            await pool.close()
    finally:
        await connection.close()


@db
async def test_the_default_team_is_the_oldest_one_in_the_callers_workspace(wired):
    """Both halves of the temporary tenant seam, against a real server.

    The scope assertion is also what proves BOOTSTRAP_WORKSPACE_SLUG names a
    workspace 002 actually seeds: it is resolved through the real
    WorkspaceService, so a slug that matched nothing would raise here rather
    than reach the team lookup.
    """
    context, _ = wired

    scope = await context.tenant.scope()

    assert scope.workspace_id == BOOTSTRAP_WORKSPACE_ID

    team_id = await context.tenant.team_id(scope)

    assert team_id == BOOTSTRAP_TEAM_ID
    assert team_id != OTHER_TEAM_ID
    assert team_id != LATER_TEAM_ID


@db
async def test_issue_create_succeeds_against_the_post_002_schema(wired):
    """The regression this phase exists to fix, over the real mutation.

    Before the repository was made tenant-aware, this exact mutation raised
    NotNullViolationError against a 002 database because the INSERT named
    only title, description and priority. The masking layer turns that into
    "Internal server error", so the assertions look for the created issue
    rather than for the absence of the old exception -- a mutation that
    still failed would come back with `errors` set and `data` null.
    """
    context, connection = wired

    result = await schema.execute(
        ISSUE_CREATE_MUTATION,
        variable_values={
            "input": {
                "title": "Filed through GraphQL",
                "description": "described",
                "priority": 2,
            }
        },
        context_value=context,
    )

    assert result.errors is None
    assert result.data is not None

    payload = result.data["issueCreate"]

    assert payload["errors"] == []
    assert payload["issue"]["title"] == "Filed through GraphQL"

    # Where the row actually landed, read from the two columns the API never
    # returns. An entity alone cannot witness its own tenant.
    stored = await connection.fetchrow(
        "SELECT workspace_id, team_id FROM issues WHERE id = $1",
        UUID(payload["issue"]["id"]),
    )

    assert stored["workspace_id"] == BOOTSTRAP_WORKSPACE_ID
    assert stored["team_id"] == BOOTSTRAP_TEAM_ID


@db
async def test_the_created_issue_is_visible_to_the_query_that_follows(wired):
    """Create then list, through the same context, as a client would.

    The write path and the read path resolve the workspace independently --
    two calls to `RequestTenant.scope`, two lookups -- so an issue written
    into one tenant and listed from another would return an empty page here
    while both halves passed their own tests.
    """
    context, _ = wired

    created = await schema.execute(
        ISSUE_CREATE_MUTATION,
        variable_values={
            "input": {"title": "Round trip", "description": None, "priority": 0}
        },
        context_value=context,
    )

    assert created.errors is None

    listed = await schema.execute(
        "{ issues(first: 10) { nodes { id title } } }",
        context_value=context,
    )

    assert listed.errors is None
    assert listed.data is not None

    nodes = listed.data["issues"]["nodes"]

    assert [node["id"] for node in nodes] == [
        created.data["issueCreate"]["issue"]["id"]
    ]
    assert nodes[0]["title"] == "Round trip"
