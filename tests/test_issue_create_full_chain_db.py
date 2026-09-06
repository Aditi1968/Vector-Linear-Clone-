"""Creating an issue against the whole migration chain, not a prefix of it.

Every other db suite that creates an issue builds the schema from a subset of
`migrations/` -- 001 alone, or 001 and 002 -- because each was written while
that was the whole chain. That is reasonable for a test whose subject is one
migration, and it left exactly one thing untested: whether the application can
still insert a row once *every* migration has run.

It could not. 005 added `issues.number` and `issues.workflow_state_id`, both
NOT NULL with no default, and `IssueRepository.create` supplied neither. Every
`issueCreate` against an integrated database failed with a not-null violation,
while 848 tests passed -- because no test had a fully migrated `issues` table
and an application insert in the same process.

So this file is deliberately not about a migration. It applies whatever is in
`migrations/`, in order, through the real runner, and then asks the service
layer to do the one thing the product exists to do. It has no list of versions
in it: a migration that breaks inserts should fail here on the day it lands,
without anyone remembering to add it to a constant.

Marked `db`: deselected by default, skipped when Docker is unreachable.
"""

from pathlib import Path
from uuid import UUID

import asyncpg
import pytest

from app.domain.tenancy import WorkspaceScope
from app.repositories.issues import IssueRepository
from app.repositories.teams import TeamRepository
from app.services.issues import IssueService
from app.services.teams import TeamService
from scripts.apply_migration import apply_migration

from tests.conftest import reset_schema


pytestmark = pytest.mark.db

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"

# The bootstrap tenant 002 seeds. Named here because this suite files issues
# into it rather than creating a workspace of its own -- it is the tenant the
# running application uses locally, so it is the one worth proving against.
BOOTSTRAP_WORKSPACE_ID = UUID("00000000-0000-7000-8000-000000000001")

SCOPE = WorkspaceScope(workspace_id=BOOTSTRAP_WORKSPACE_ID)

# The category 005 places a not-yet-completed issue in. Asserted as a category
# rather than as the name 'Todo', for the reason 005 gives: a team may rename
# the state, and renaming it must not break issue creation.
NEW_ISSUE_CATEGORY = "unstarted"

ISSUE_ROW_SQL = """
SELECT
    issues.number,
    issues.workflow_state_id,
    state.type AS category,
    teams.key AS team_key
FROM issues
JOIN workflow_states AS state ON state.id = issues.workflow_state_id
JOIN teams ON teams.id = issues.team_id
WHERE issues.id = $1
"""


async def apply_every_migration(connection) -> list[str]:
    """Apply `migrations/` in filename order, through the real runner.

    Globbed rather than listed. The point of this suite is that it keeps
    testing the whole chain as the chain grows; a hand-written list here
    would freeze it at the day it was written, which is the failure this
    file exists because of.
    """
    applied = []

    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        async with connection.transaction():
            await apply_migration(connection, path, migrations_dir=MIGRATIONS_DIR)

        applied.append(path.name)

    return applied


@pytest.fixture
async def migrated_pool(postgres_dsn):
    """A pool over a database with every migration applied."""
    connection = await asyncpg.connect(postgres_dsn)

    try:
        await reset_schema(connection)
        await apply_every_migration(connection)
    finally:
        await connection.close()

    pool = await asyncpg.create_pool(dsn=postgres_dsn, min_size=1, max_size=2)

    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
def team_service(migrated_pool):
    return TeamService(pool=migrated_pool, repository=TeamRepository())


@pytest.fixture
def issue_service(migrated_pool, team_service):
    """Wired exactly as `get_context` wires it, team service included.

    The same instance the tests use directly, not a second one: creating an
    issue and reading the counter afterwards have to see one service's view
    of the database.
    """
    return IssueService(
        pool=migrated_pool,
        repository=IssueRepository(),
        teams=team_service,
    )


async def test_every_migration_applies_in_order(migrated_pool):
    """The chain itself, before anything is asserted about the application.

    If this fails, every other failure in this file is a consequence rather
    than a finding.
    """
    async with migrated_pool.acquire() as connection:
        versions = await connection.fetch(
            "SELECT version FROM schema_migrations ORDER BY version"
        )

    on_disk = sorted(path.name.split("_")[0] for path in MIGRATIONS_DIR.glob("*.sql"))

    assert [row["version"] for row in versions] == on_disk


async def test_creating_an_issue_succeeds_on_a_fully_migrated_database(
    issue_service, team_service, migrated_pool
):
    """The regression this file was written for.

    Before `IssueService.create` allocated a number and resolved a workflow
    state, this raised NotNullViolationError on `issues.number`.
    """
    team_id = await team_service.default_team_id(SCOPE)

    entity = await issue_service.create(
        scope=SCOPE,
        team_id=team_id,
        title="A first issue on the integrated schema",
        description=None,
        priority=0,
    )

    async with migrated_pool.acquire() as connection:
        row = await connection.fetchrow(ISSUE_ROW_SQL, entity.id)

    assert row is not None, "the issue was not written"
    assert row["number"] >= 1
    assert row["workflow_state_id"] is not None

    # By category, never by name: 005 maps status onto a category precisely
    # so that renaming 'Todo' does not change where a new issue lands.
    assert row["category"] == NEW_ISSUE_CATEGORY


async def test_numbers_are_allocated_in_sequence_without_gaps(
    issue_service, team_service, migrated_pool
):
    """Consecutive creates take consecutive numbers.

    The identifier a person types (`CORE-4`) is only stable if the counter
    advances by exactly one per issue. Asserted over three creates rather
    than two, so an allocator that returned a constant and an allocator that
    doubled are both caught.

    Read back over SQL rather than off the entity, because `IssueEntity`
    does not carry the number: exposing it through the domain and the schema
    is issue-core's work and is not on main yet. What is on main is the
    column, and the column is what this checks.
    """
    team_id = await team_service.default_team_id(SCOPE)

    created = []

    for index in range(3):
        entity = await issue_service.create(
            scope=SCOPE,
            team_id=team_id,
            title=f"Sequential issue {index}",
            description=None,
            priority=0,
        )
        created.append(entity.id)

    async with migrated_pool.acquire() as connection:
        numbers = [
            await connection.fetchval(
                "SELECT number FROM issues WHERE id = $1", issue_id
            )
            for issue_id in created
        ]

    assert numbers == [numbers[0], numbers[0] + 1, numbers[0] + 2]


async def test_a_failed_insert_does_not_consume_a_number(
    issue_service, team_service, migrated_pool
):
    """The counter and the row commit together or not at all.

    `allocate_issue_number` documents that the increment must live in the
    transaction that inserts, so that a failed insert returns its number
    rather than burning it -- the property a sequence could not have, since
    `nextval()` is exempt from rollback. That is a claim about a rollback,
    and only a rollback can check it.

    The failure has to happen *after* the allocation, which rules out the
    obvious ways to cause one: an invalid title is rejected before a
    connection is taken, and a team from another workspace is refused by the
    state lookup one statement earlier. So the insert itself is made to
    fail, which is exactly the moment the guarantee is about.
    """
    team_id = await team_service.default_team_id(SCOPE)

    async with migrated_pool.acquire() as connection:
        before = await connection.fetchval(
            "SELECT issue_counter FROM teams WHERE id = $1", team_id
        )

    class FailingRepository(IssueRepository):
        async def create(self, *args, **kwargs):
            raise RuntimeError("the insert failed after the number was claimed")

    breaking_service = IssueService(
        pool=migrated_pool,
        repository=FailingRepository(),
        teams=team_service,
    )

    with pytest.raises(RuntimeError):
        await breaking_service.create(
            scope=SCOPE,
            team_id=team_id,
            title="Allocated a number, then failed to insert",
            description=None,
            priority=0,
        )

    async with migrated_pool.acquire() as connection:
        after = await connection.fetchval(
            "SELECT issue_counter FROM teams WHERE id = $1", team_id
        )

    assert after == before, (
        "the rolled-back allocation burned a number; the increment and the "
        "insert are no longer in one transaction"
    )


async def test_the_issue_is_visible_to_the_list_that_follows(
    issue_service, team_service
):
    """Create then read, through the service both times.

    The list query gained no new columns in 005, but it selects from a table
    that did. A create that succeeds and a list that then fails to decode the
    row is a real shape of failure, and it is invisible to a test that only
    creates.
    """
    team_id = await team_service.default_team_id(SCOPE)

    created = await issue_service.create(
        scope=SCOPE,
        team_id=team_id,
        title="Visible to the list",
        description=None,
        priority=0,
    )

    page = await issue_service.list(scope=SCOPE, first=25, after=None)

    assert created.id in {issue.id for issue in page.nodes}
