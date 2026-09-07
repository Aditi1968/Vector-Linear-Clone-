"""Migration 013 applied by the real runner, then asked what it built.

Most of this migration is ordinary. Four things are not, and each is one word
away from a version that reads correctly in a diff and is wrong in production:

  * `connected_by UUID REFERENCES users (id)` is the natural spelling and it
    records any account in the system as the person who connected any
    workspace's integration. The reference has to be the composite
    `(workspace_id, connected_by) -> workspace_members (workspace_id, user_id)`,
    so the database itself refuses a connector who is not a member of the
    workspace they connected;
  * `UNIQUE (installation_id)` is what stops two workspaces claiming one GitHub
    installation. Without it a webhook delivery has two possible destinations
    and the planner picks, which is another tenant's repository names in
    somebody's settings page;
  * `ON DELETE CASCADE` on the repositories would let a one-line delete against
    `github_installations` discard rows in a second table while reporting
    `DELETE 1`;
  * the table has no column that can hold a token, a key or a secret, and that
    is asserted rather than assumed -- a `SELECT *` two refactors from now is
    all it takes for a column added quietly to reach a client.

The last is the one a catalog listing cannot show by omission, so this file
asks the server for the column list and pins it.

Marked `db`: deselected by default, skipped when Docker is unreachable.
"""

from datetime import timedelta
from uuid import UUID

import asyncpg
import pytest

from app.domain.github import GithubRepositoryEntity
from app.domain.tenancy import WorkspaceScope
from app.repositories.github import GithubRepository

from tests.conftest import apply_all_migrations, reset_schema


pytestmark = pytest.mark.db

# The tenant 002 seeds, named there as literals precisely so a test can assert
# against a constant instead of querying for the value it is about to check.
BOOTSTRAP_WORKSPACE_ID = UUID("00000000-0000-7000-8000-000000000001")

OTHER_WORKSPACE_ID = UUID("00000000-0000-7000-8000-0000000000a1")

# A member of the bootstrap workspace: the one account that may be recorded as
# having connected its integration.
MEMBER_ID = UUID("00000000-0000-7000-8000-0000000000e1")

# A real account that belongs to the OTHER workspace. This is the id the naive
# `REFERENCES users (id)` would accept as the connector of a bootstrap
# installation, and the composite key refuses. It has to be a real user with a
# real membership, because a nonexistent id would be refused by either spelling
# and would prove nothing about which one is in force.
OUTSIDER_ID = UUID("00000000-0000-7000-8000-0000000000e2")

INSTALLATION_ID = 4242

# The single-byte codes pg_constraint stores for referential actions, pinned as
# bytes for the reason tests/test_migration_002_db.py gives: the byte is what
# the executor consults, and pinning it stops RESTRICT drifting into CASCADE
# behind a rendering that still reads plausibly.
RESTRICT = "r"

INSERT_USER_SQL = """
INSERT INTO users (id, email, password_hash)
VALUES (
    $1::uuid,
    'user-' || $1::text || '@example.test',
    '$argon2id$not-a-real-hash'
)
"""

INSERT_INSTALLATION_SQL = """
INSERT INTO github_installations (workspace_id, installation_id, connected_by)
VALUES ($1, $2, $3)
"""

INSERT_REPOSITORY_SQL = """
INSERT INTO github_repositories (workspace_id, repository_id, full_name)
VALUES ($1, $2, $3)
"""

# What a signed delivery does, spelled out here so that a test whose subject is
# one of 013's own constraints can get past 016's. See
# tests/test_migration_016_db.py for what this column means.
CONFIRM_INSTALLATION_SQL = """
UPDATE github_installations SET confirmed_at = now() WHERE workspace_id = $1
"""

# Columns and referenced columns of one foreign key, in key order. The two
# column arrays are what catch the single-column version of the same
# constraint -- the failure no rendering of the table makes obvious.
FOREIGN_KEY_SQL = """
SELECT
    confrelid::regclass::text AS referenced_table,
    confupdtype::text AS update_action,
    confdeltype::text AS delete_action,
    (
        SELECT array_agg(att.attname ORDER BY key_column.ord)
        FROM unnest(con.conkey) WITH ORDINALITY AS key_column(attnum, ord)
        JOIN pg_attribute att
            ON att.attrelid = con.conrelid AND att.attnum = key_column.attnum
    ) AS referencing_columns,
    (
        SELECT array_agg(att.attname ORDER BY key_column.ord)
        FROM unnest(con.confkey) WITH ORDINALITY AS key_column(attnum, ord)
        JOIN pg_attribute att
            ON att.attrelid = con.confrelid AND att.attnum = key_column.attnum
    ) AS referenced_columns
FROM pg_constraint con
WHERE con.conname = $1 AND con.contype = 'f'
"""

COLUMNS_SQL = """
SELECT column_name
FROM information_schema.columns
WHERE table_schema = 'public' AND table_name = $1
ORDER BY column_name
"""


@pytest.fixture
async def connection(postgres_dsn):
    """A migrated database, torn back down to nothing afterwards.

    Every db file in this suite shares one container, so leaving tables behind
    would break whichever file ran next on a DROP it never wrote.
    """
    conn = await asyncpg.connect(postgres_dsn)

    try:
        await reset_schema(conn)
        await apply_all_migrations(conn)
        await seed(conn)

        yield conn
    finally:
        await reset_schema(conn)
        await conn.close()


async def seed(connection) -> None:
    """Two workspaces, two accounts, one membership each."""
    await connection.execute(
        """
        INSERT INTO workspaces (id, slug, name)
        VALUES ($1, 'other', 'Other')
        """,
        OTHER_WORKSPACE_ID,
    )

    for user_id in (MEMBER_ID, OUTSIDER_ID):
        await connection.execute(INSERT_USER_SQL, user_id)

    await connection.execute(
        """
        INSERT INTO workspace_members (workspace_id, user_id, role)
        VALUES ($1, $2, 'admin')
        """,
        BOOTSTRAP_WORKSPACE_ID,
        MEMBER_ID,
    )
    await connection.execute(
        """
        INSERT INTO workspace_members (workspace_id, user_id, role)
        VALUES ($1, $2, 'admin')
        """,
        OTHER_WORKSPACE_ID,
        OUTSIDER_ID,
    )


# --- the shape --------------------------------------------------------


async def test_the_connector_is_checked_against_the_workspace_not_the_users(
    connection,
):
    """The composite key, read off the catalog.

    A single-column `(connected_by) -> users (id)` satisfies every test that
    only inserts valid rows, so the columns are asserted rather than the
    constraint's existence.
    """
    key = await connection.fetchrow(
        FOREIGN_KEY_SQL, "github_installations_connected_by_fk"
    )

    assert key is not None
    assert key["referenced_table"] == "workspace_members"
    assert list(key["referencing_columns"]) == ["workspace_id", "connected_by"]
    assert list(key["referenced_columns"]) == ["workspace_id", "user_id"]
    assert key["delete_action"] == RESTRICT
    assert key["update_action"] == RESTRICT


async def test_the_repositories_restrict_rather_than_cascade(connection):
    key = await connection.fetchrow(
        FOREIGN_KEY_SQL, "github_repositories_installation_fk"
    )

    assert key is not None
    assert key["referenced_table"] == "github_installations"
    assert key["delete_action"] == RESTRICT
    assert key["update_action"] == RESTRICT


async def test_the_installation_table_holds_no_credential(connection):
    """The column list, pinned.

    A GitHub App's private key, its client secret, its webhook secret and any
    installation token it mints live in configuration and nowhere else. This is
    the assertion that says so about the storage rather than about the code
    that currently writes it.
    """
    columns = [
        row["column_name"]
        for row in await connection.fetch(COLUMNS_SQL, "github_installations")
    ]

    # `confirmed_at` is 016's, and the fixture applies every migration. It is
    # in this list rather than only in tests/test_migration_016_db.py because
    # the point of this assertion is the whole column list: a column added
    # anywhere is one `SELECT *` from a client, and that is true whichever
    # migration added it.
    assert columns == [
        "account_login",
        "confirmed_at",
        "connected_at",
        "connected_by",
        "installation_id",
        "updated_at",
        "workspace_id",
    ]


async def test_the_repository_table_holds_no_credential(connection):
    columns = [
        row["column_name"]
        for row in await connection.fetch(COLUMNS_SQL, "github_repositories")
    ]

    assert columns == ["created_at", "full_name", "repository_id", "workspace_id"]


# --- what the shape refuses -------------------------------------------


async def test_a_connector_from_another_workspace_is_refused(connection):
    """The bug the composite key exists to make unstorable.

    OUTSIDER_ID is a real account with a real membership -- of the other
    workspace. `REFERENCES users (id)` would accept this row.
    """
    with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
        await connection.execute(
            INSERT_INSTALLATION_SQL,
            BOOTSTRAP_WORKSPACE_ID,
            INSTALLATION_ID,
            OUTSIDER_ID,
        )

    assert raised.value.constraint_name == "github_installations_connected_by_fk"


async def test_one_installation_cannot_be_claimed_by_two_workspaces(connection):
    """Webhook routing is by installation id, so the claim has to be exclusive.

    Both rows are legal on their own: each connector is a member of the
    workspace they are recorded against. Only the shared installation id is
    wrong, which is exactly what the unique constraint is for.
    """
    await connection.execute(
        INSERT_INSTALLATION_SQL, BOOTSTRAP_WORKSPACE_ID, INSTALLATION_ID, MEMBER_ID
    )

    with pytest.raises(asyncpg.UniqueViolationError) as raised:
        await connection.execute(
            INSERT_INSTALLATION_SQL, OTHER_WORKSPACE_ID, INSTALLATION_ID, OUTSIDER_ID
        )

    assert raised.value.constraint_name == ("github_installations_installation_id_key")


async def test_a_workspace_connects_at_most_one_installation(connection):
    await connection.execute(
        INSERT_INSTALLATION_SQL, BOOTSTRAP_WORKSPACE_ID, INSTALLATION_ID, MEMBER_ID
    )

    with pytest.raises(asyncpg.UniqueViolationError):
        await connection.execute(
            INSERT_INSTALLATION_SQL, BOOTSTRAP_WORKSPACE_ID, 9999, MEMBER_ID
        )


async def test_deleting_an_installation_with_repositories_is_refused(connection):
    """RESTRICT, so a one-line delete cannot silently empty a second table.

    RestrictViolationError specifically, not the ForeignKeyViolationError an
    insert raises: PostgreSQL reports RESTRICT with its own SQLSTATE, and NO
    ACTION -- which defers to the end of the statement and would let a
    same-statement cleanup slip through -- reports the other. The distinct
    exception is the evidence that RESTRICT is what is in force.
    """
    await connection.execute(
        INSERT_INSTALLATION_SQL, BOOTSTRAP_WORKSPACE_ID, INSTALLATION_ID, MEMBER_ID
    )
    await connection.execute(
        INSERT_REPOSITORY_SQL, BOOTSTRAP_WORKSPACE_ID, 7, "acme/web"
    )

    with pytest.raises(asyncpg.RestrictViolationError):
        await connection.execute(
            "DELETE FROM github_installations WHERE workspace_id = $1",
            BOOTSTRAP_WORKSPACE_ID,
        )


async def test_removing_the_connector_while_connected_is_refused(connection):
    """RESTRICT from the membership side, matching projects.lead_id in 009."""
    await connection.execute(
        INSERT_INSTALLATION_SQL, BOOTSTRAP_WORKSPACE_ID, INSTALLATION_ID, MEMBER_ID
    )

    with pytest.raises(asyncpg.RestrictViolationError):
        await connection.execute(
            "DELETE FROM workspace_members WHERE workspace_id = $1 AND user_id = $2",
            BOOTSTRAP_WORKSPACE_ID,
            MEMBER_ID,
        )


@pytest.mark.parametrize("installation_id", [0, -1])
async def test_a_non_positive_installation_id_is_refused(connection, installation_id):
    """An empty query parameter read as 0 is not an installation."""
    with pytest.raises(asyncpg.CheckViolationError):
        await connection.execute(
            INSERT_INSTALLATION_SQL,
            BOOTSTRAP_WORKSPACE_ID,
            installation_id,
            MEMBER_ID,
        )


@pytest.mark.parametrize(
    "login",
    ["", "-leading-hyphen", "has space", "has/slash", "a" * 40],
)
async def test_an_account_login_that_is_not_one_is_refused(connection, login):
    """The FORMAT check, which needs a confirmed row to be the one that fires.

    016 added `github_installations_unconfirmed_holds_no_account`, and against
    a fresh claim that constraint refuses every login including a well-formed
    one -- so without the confirmation below this test would pass while proving
    nothing about the format rule. The constraint name is asserted for the same
    reason: two CheckViolationErrors are not the same evidence.
    """
    await connection.execute(
        INSERT_INSTALLATION_SQL, BOOTSTRAP_WORKSPACE_ID, INSTALLATION_ID, MEMBER_ID
    )
    await connection.execute(CONFIRM_INSTALLATION_SQL, BOOTSTRAP_WORKSPACE_ID)

    with pytest.raises(asyncpg.CheckViolationError) as raised:
        await connection.execute(
            "UPDATE github_installations SET account_login = $2 WHERE workspace_id = $1",
            BOOTSTRAP_WORKSPACE_ID,
            login,
        )

    assert raised.value.constraint_name == "github_installations_account_login_format"


async def test_an_account_login_may_be_absent(connection):
    """NULL is the state between the callback and the first webhook."""
    await connection.execute(
        INSERT_INSTALLATION_SQL, BOOTSTRAP_WORKSPACE_ID, INSTALLATION_ID, MEMBER_ID
    )

    assert (
        await connection.fetchval(
            "SELECT account_login FROM github_installations WHERE workspace_id = $1",
            BOOTSTRAP_WORKSPACE_ID,
        )
        is None
    )


@pytest.mark.parametrize(
    "full_name",
    ["", "web", "acme/", "/web", "acme/web/extra", "acme /web", "https://x/y"],
)
async def test_a_full_name_that_is_not_owner_slash_repo_is_refused(
    connection, full_name
):
    await connection.execute(
        INSERT_INSTALLATION_SQL, BOOTSTRAP_WORKSPACE_ID, INSTALLATION_ID, MEMBER_ID
    )

    with pytest.raises(asyncpg.CheckViolationError):
        await connection.execute(
            INSERT_REPOSITORY_SQL, BOOTSTRAP_WORKSPACE_ID, 7, full_name
        )


async def test_a_repository_needs_an_installation(connection):
    """No orphan repositories: the parent row is what a delivery routes to."""
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await connection.execute(
            INSERT_REPOSITORY_SQL, BOOTSTRAP_WORKSPACE_ID, 7, "acme/web"
        )


# --- the repository's real SQL ----------------------------------------


async def test_the_repository_round_trips_an_installation(connection):
    """Every statement in GithubRepository, against the schema it was written
    for. The fakes elsewhere in this suite record calls; this is what proves
    the calls they record would have worked."""
    repository = GithubRepository()
    scope = WorkspaceScope(workspace_id=BOOTSTRAP_WORKSPACE_ID)

    installation = await repository.insert_installation(
        connection,
        scope=scope,
        installation_id=INSTALLATION_ID,
        connected_by=MEMBER_ID,
    )

    assert installation.installation_id == INSTALLATION_ID
    assert installation.account_login is None
    assert installation.connected_by == MEMBER_ID

    # A fresh row is a claim, so it resolves no delivery yet. Confirming it is
    # what 016 added and tests/test_migration_016_db.py is about; here it is
    # the step that makes the rest of the round trip reachable at all.
    assert (
        await repository.find_confirmed_workspace_by_installation_id(
            connection, installation_id=INSTALLATION_ID
        )
        is None
    )
    assert (
        await repository.confirm_installation(
            connection,
            installation_id=INSTALLATION_ID,
            within=timedelta(minutes=15),
        )
        == BOOTSTRAP_WORKSPACE_ID
    )

    found = await repository.find_confirmed_workspace_by_installation_id(
        connection, installation_id=INSTALLATION_ID
    )

    assert found == BOOTSTRAP_WORKSPACE_ID

    await repository.set_account_login(connection, scope=scope, account_login="acme")
    await repository.add_repositories(
        connection,
        scope=scope,
        repositories=[
            GithubRepositoryEntity(repository_id=8, full_name="acme/api"),
            GithubRepositoryEntity(repository_id=7, full_name="acme/web"),
        ],
    )

    assert await repository.list_repositories(connection, scope=scope) == [
        GithubRepositoryEntity(repository_id=8, full_name="acme/api"),
        GithubRepositoryEntity(repository_id=7, full_name="acme/web"),
    ]

    reread = await repository.get_installation(connection, scope=scope)

    assert reread is not None
    assert reread.account_login == "acme"

    # A subset by id, then the rest with no id list at all -- the two halves of
    # the one statement `delete_repositories` uses.
    await repository.delete_repositories(connection, scope=scope, repository_ids=[8])

    assert [
        one.repository_id
        for one in await repository.list_repositories(connection, scope=scope)
    ] == [7]

    await repository.delete_repositories(connection, scope=scope)

    assert await repository.list_repositories(connection, scope=scope) == []
    assert await repository.delete_installation(connection, scope=scope) is True
    assert await repository.get_installation(connection, scope=scope) is None
    assert await repository.delete_installation(connection, scope=scope) is False


async def test_a_repository_read_cannot_reach_another_tenant(connection):
    """The scope is in the predicate, so another workspace's rows do not come
    back -- and there is no id argument for a caller to aim elsewhere."""
    repository = GithubRepository()

    await connection.execute(
        INSERT_INSTALLATION_SQL, BOOTSTRAP_WORKSPACE_ID, INSTALLATION_ID, MEMBER_ID
    )
    await connection.execute(
        INSERT_REPOSITORY_SQL, BOOTSTRAP_WORKSPACE_ID, 7, "acme/web"
    )

    other = WorkspaceScope(workspace_id=OTHER_WORKSPACE_ID)

    assert await repository.get_installation(connection, scope=other) is None
    assert await repository.list_repositories(connection, scope=other) == []

    # And a delete aimed at the other workspace removes nothing of this one's.
    await repository.delete_repositories(connection, scope=other)

    assert await repository.delete_installation(connection, scope=other) is False
    assert await connection.fetchval("SELECT count(*) FROM github_repositories") == 1


async def test_the_repository_reports_an_installation_another_workspace_holds(
    connection,
):
    """The unique constraint, translated into a domain error by the repository
    rather than escaping as an asyncpg exception."""
    from app.domain.errors import GithubInstallationClaimedError

    repository = GithubRepository()

    await connection.execute(
        INSERT_INSTALLATION_SQL, BOOTSTRAP_WORKSPACE_ID, INSTALLATION_ID, MEMBER_ID
    )

    with pytest.raises(GithubInstallationClaimedError):
        await repository.insert_installation(
            connection,
            scope=WorkspaceScope(workspace_id=OTHER_WORKSPACE_ID),
            installation_id=INSTALLATION_ID,
            connected_by=OUTSIDER_ID,
        )
