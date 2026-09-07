"""Migration 016 applied by the real runner, then asked what it built.

016 exists because 013 stored a claim and read it as a fact. The install
callback took `installation_id` from a query string, wrote a row, and every
later delivery for that installation resolved to whoever wrote it -- so an
admin of their own workspace could name another organisation's installation
and receive that organisation's account login and private repository names.
GitHub numbers installations with a small ascending counter, so the guess was
cheap, and 013's unique constraint made the first guess permanent.

Three things carry the fix and each is asserted here against a real server
rather than against a fake:

  * `confirmed_at` distinguishes "a workspace said this" from "GitHub said
    this", and a claim is born with it NULL;
  * `github_installations_unconfirmed_holds_no_account` makes the leak
    unstorable rather than merely unwritten -- an unconfirmed row cannot hold
    an account login at all, so a claim nobody ever confirms accumulates
    nothing a client could read;
  * the three statements that route a delivery -- resolve, confirm, sweep --
    do what their docstrings say against the schema they were written for,
    including the window that makes a stale claim unconfirmable and the sweep
    that stops one blocking the real owner for good.

Marked `db`: deselected by default, skipped when Docker is unreachable.
"""

from datetime import timedelta
from uuid import UUID

import asyncpg
import pytest

from app.domain.errors import GithubInstallationClaimedError
from app.domain.tenancy import WorkspaceScope
from app.repositories.github import GithubRepository
from scripts.apply_migration import apply_migration

from tests.conftest import MIGRATIONS_DIR, apply_all_migrations, reset_schema


pytestmark = pytest.mark.db

THIS_MIGRATION = MIGRATIONS_DIR / "016_github_installation_trust.sql"

# The tenant 002 seeds, named there as literals precisely so a test can assert
# against a constant instead of querying for the value it is about to check.
BOOTSTRAP_WORKSPACE_ID = UUID("00000000-0000-7000-8000-000000000001")

# The attacker in the story below: a real workspace whose admin is entitled to
# start an install of their own, and whose claim on somebody else's
# installation must therefore be refused by the schema rather than by a role.
OTHER_WORKSPACE_ID = UUID("00000000-0000-7000-8000-0000000000a1")

MEMBER_ID = UUID("00000000-0000-7000-8000-0000000000e1")
OUTSIDER_ID = UUID("00000000-0000-7000-8000-0000000000e2")

INSTALLATION_ID = 4242

# The window `app.services.github.CLAIM_TTL` holds. Restated rather than
# imported, so that shortening the policy does not silently rewrite what these
# tests mean by "young" and "stale".
WINDOW = timedelta(minutes=15)

INSERT_USER_SQL = """
INSERT INTO users (id, email, password_hash)
VALUES (
    $1::uuid,
    'user-' || $1::text || '@example.test',
    '$argon2id$not-a-real-hash'
)
"""

INSERT_CLAIM_SQL = """
INSERT INTO github_installations (workspace_id, installation_id, connected_by)
VALUES ($1, $2, $3)
"""

# A claim written in the past, which is how a test gets an expired one without
# waiting a quarter of an hour. The interval is a parameter so the row's age is
# the test's subject rather than a literal buried in SQL.
AGE_CLAIM_SQL = """
UPDATE github_installations
SET connected_at = now() - $2::interval
WHERE workspace_id = $1
"""

CONFIRMED_AT_SQL = """
SELECT confirmed_at FROM github_installations WHERE workspace_id = $1
"""


@pytest.fixture
async def connection(postgres_dsn):
    """A migrated database, torn back down to nothing afterwards."""
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
    """Two workspaces, two admins, one membership each."""
    await connection.execute(
        """
        INSERT INTO workspaces (id, slug, name)
        VALUES ($1, 'other', 'Other')
        """,
        OTHER_WORKSPACE_ID,
    )

    for user_id in (MEMBER_ID, OUTSIDER_ID):
        await connection.execute(INSERT_USER_SQL, user_id)

    for workspace_id, user_id in (
        (BOOTSTRAP_WORKSPACE_ID, MEMBER_ID),
        (OTHER_WORKSPACE_ID, OUTSIDER_ID),
    ):
        await connection.execute(
            """
            INSERT INTO workspace_members (workspace_id, user_id, role)
            VALUES ($1, $2, 'admin')
            """,
            workspace_id,
            user_id,
        )


# --- the shape --------------------------------------------------------


async def test_a_claim_is_born_unconfirmed(connection):
    """The default is the whole point: nothing is connected by being written."""
    await connection.execute(
        INSERT_CLAIM_SQL, BOOTSTRAP_WORKSPACE_ID, INSTALLATION_ID, MEMBER_ID
    )

    assert await connection.fetchval(CONFIRMED_AT_SQL, BOOTSTRAP_WORKSPACE_ID) is None


async def test_an_unconfirmed_claim_cannot_hold_an_account_login(connection):
    """The leak, refused by the database rather than by the writer.

    `account_login` is somebody's organisation name and it arrives only from a
    webhook. A claim that was never confirmed holding one would mean an
    attacker who guessed an installation id could read the victim's
    organisation out of `githubIntegration`, which is the defect surviving its
    own fix.
    """
    await connection.execute(
        INSERT_CLAIM_SQL, BOOTSTRAP_WORKSPACE_ID, INSTALLATION_ID, MEMBER_ID
    )

    with pytest.raises(asyncpg.CheckViolationError) as raised:
        await connection.execute(
            "UPDATE github_installations SET account_login = $2 WHERE workspace_id = $1",
            BOOTSTRAP_WORKSPACE_ID,
            "victim-org",
        )

    assert raised.value.constraint_name == (
        "github_installations_unconfirmed_holds_no_account"
    )


async def test_a_confirmed_installation_may_hold_one(connection):
    """The same constraint from the other side, so it is not merely a ban."""
    repository = GithubRepository()
    scope = WorkspaceScope(workspace_id=BOOTSTRAP_WORKSPACE_ID)

    await connection.execute(
        INSERT_CLAIM_SQL, BOOTSTRAP_WORKSPACE_ID, INSTALLATION_ID, MEMBER_ID
    )
    await repository.confirm_installation(
        connection, installation_id=INSTALLATION_ID, within=WINDOW
    )
    await repository.set_account_login(connection, scope=scope, account_login="acme")

    installation = await repository.get_installation(connection, scope=scope)

    assert installation is not None
    assert installation.account_login == "acme"
    assert installation.confirmed_at is not None


# --- routing a delivery -----------------------------------------------


async def test_an_unconfirmed_claim_resolves_no_delivery(connection):
    """The choke point. Every webhook write in this feature is downstream of
    this lookup, so a claim answering None here writes nothing anywhere."""
    repository = GithubRepository()

    await connection.execute(
        INSERT_CLAIM_SQL, BOOTSTRAP_WORKSPACE_ID, INSTALLATION_ID, MEMBER_ID
    )

    assert (
        await repository.find_confirmed_workspace_by_installation_id(
            connection, installation_id=INSTALLATION_ID
        )
        is None
    )


async def test_a_confirmation_promotes_exactly_the_claiming_workspace(connection):
    repository = GithubRepository()

    await connection.execute(
        INSERT_CLAIM_SQL, BOOTSTRAP_WORKSPACE_ID, INSTALLATION_ID, MEMBER_ID
    )

    assert (
        await repository.confirm_installation(
            connection, installation_id=INSTALLATION_ID, within=WINDOW
        )
        == BOOTSTRAP_WORKSPACE_ID
    )
    assert (
        await repository.find_confirmed_workspace_by_installation_id(
            connection, installation_id=INSTALLATION_ID
        )
        == BOOTSTRAP_WORKSPACE_ID
    )


async def test_a_confirmation_for_an_installation_nobody_claimed_promotes_nothing(
    connection,
):
    """A signed delivery is evidence about an installation, not a licence to
    create a workspace's link to one out of nothing."""
    repository = GithubRepository()

    assert (
        await repository.confirm_installation(
            connection, installation_id=INSTALLATION_ID, within=WINDOW
        )
        is None
    )
    assert await connection.fetchval("SELECT count(*) FROM github_installations") == 0


async def test_a_claim_belonging_to_another_workspace_is_not_promoted_for_this_one(
    connection,
):
    """There is no `workspace_id` argument to aim: the statement promotes the
    one claim that holds the id, and answers whose it was."""
    repository = GithubRepository()

    await connection.execute(
        INSERT_CLAIM_SQL, OTHER_WORKSPACE_ID, INSTALLATION_ID, OUTSIDER_ID
    )

    assert (
        await repository.confirm_installation(
            connection, installation_id=INSTALLATION_ID, within=WINDOW
        )
        == OTHER_WORKSPACE_ID
    )
    assert (
        await repository.get_installation(
            connection, scope=WorkspaceScope(workspace_id=BOOTSTRAP_WORKSPACE_ID)
        )
        is None
    )


async def test_a_stale_claim_is_not_promoted_by_a_late_delivery(connection):
    """The window, which is the defence.

    A claim on an installation created months ago is confirmed by nothing: the
    deliveries that could have confirmed it were sent and dropped before the
    claim existed. Wound forward here by ageing the row rather than by waiting.
    """
    repository = GithubRepository()

    await connection.execute(
        INSERT_CLAIM_SQL, BOOTSTRAP_WORKSPACE_ID, INSTALLATION_ID, MEMBER_ID
    )
    await connection.execute(
        AGE_CLAIM_SQL, BOOTSTRAP_WORKSPACE_ID, WINDOW + timedelta(seconds=1)
    )

    assert (
        await repository.confirm_installation(
            connection, installation_id=INSTALLATION_ID, within=WINDOW
        )
        is None
    )
    assert await connection.fetchval(CONFIRMED_AT_SQL, BOOTSTRAP_WORKSPACE_ID) is None


async def test_a_confirmed_installation_is_not_reconfirmed(connection):
    """`confirmed_at IS NULL` in the predicate: a later delivery must not be
    able to move the timestamp, or the window would reopen on every event."""
    repository = GithubRepository()

    await connection.execute(
        INSERT_CLAIM_SQL, BOOTSTRAP_WORKSPACE_ID, INSTALLATION_ID, MEMBER_ID
    )
    await repository.confirm_installation(
        connection, installation_id=INSTALLATION_ID, within=WINDOW
    )

    first = await connection.fetchval(CONFIRMED_AT_SQL, BOOTSTRAP_WORKSPACE_ID)

    assert (
        await repository.confirm_installation(
            connection, installation_id=INSTALLATION_ID, within=WINDOW
        )
        is None
    )
    assert await connection.fetchval(CONFIRMED_AT_SQL, BOOTSTRAP_WORKSPACE_ID) == first


# --- a stale claim must not lock an id away ---------------------------


async def test_a_live_claim_refuses_a_second_workspace(connection):
    """The clean refusal. 013's unique constraint, still doing its job, and
    still the reason webhook routing is unambiguous."""
    repository = GithubRepository()

    await connection.execute(
        INSERT_CLAIM_SQL, OTHER_WORKSPACE_ID, INSTALLATION_ID, OUTSIDER_ID
    )

    assert (
        await repository.delete_expired_claim(
            connection, installation_id=INSTALLATION_ID, older_than=WINDOW
        )
        is False
    )

    with pytest.raises(GithubInstallationClaimedError):
        await repository.insert_installation(
            connection,
            scope=WorkspaceScope(workspace_id=BOOTSTRAP_WORKSPACE_ID),
            installation_id=INSTALLATION_ID,
            connected_by=MEMBER_ID,
        )


async def test_a_stale_claim_is_swept_so_the_real_owner_can_connect(connection):
    """The denial of service, closed.

    Without the sweep a single unconfirmed claim would hold an installation id
    for the lifetime of the database -- which is most of the original attack
    surviving the fix, since pre-claiming a range of ids is cheap and the real
    owner would still never get in.
    """
    repository = GithubRepository()

    await connection.execute(
        INSERT_CLAIM_SQL, OTHER_WORKSPACE_ID, INSTALLATION_ID, OUTSIDER_ID
    )
    await connection.execute(
        AGE_CLAIM_SQL, OTHER_WORKSPACE_ID, WINDOW + timedelta(seconds=1)
    )

    assert (
        await repository.delete_expired_claim(
            connection, installation_id=INSTALLATION_ID, older_than=WINDOW
        )
        is True
    )

    installation = await repository.insert_installation(
        connection,
        scope=WorkspaceScope(workspace_id=BOOTSTRAP_WORKSPACE_ID),
        installation_id=INSTALLATION_ID,
        connected_by=MEMBER_ID,
    )

    assert installation.confirmed_at is None


async def test_the_sweep_cannot_touch_a_confirmed_installation(connection):
    """Unscoped and a delete, so what it CANNOT do is the assertion worth
    having: an aged, confirmed installation is not a stale claim."""
    repository = GithubRepository()

    await connection.execute(
        INSERT_CLAIM_SQL, OTHER_WORKSPACE_ID, INSTALLATION_ID, OUTSIDER_ID
    )
    await repository.confirm_installation(
        connection, installation_id=INSTALLATION_ID, within=WINDOW
    )
    await connection.execute(AGE_CLAIM_SQL, OTHER_WORKSPACE_ID, timedelta(days=365))

    assert (
        await repository.delete_expired_claim(
            connection, installation_id=INSTALLATION_ID, older_than=WINDOW
        )
        is False
    )
    assert (
        await repository.find_confirmed_workspace_by_installation_id(
            connection, installation_id=INSTALLATION_ID
        )
        == OTHER_WORKSPACE_ID
    )


# --- what the migration did to rows that were already there -----------


async def test_the_backfill_confirms_only_what_a_webhook_had_already_named(
    postgres_dsn,
):
    """016 applied to a database that already held 013's rows.

    Under 013's code `account_login` had exactly one writer, reached only from
    a delivery whose signature had been verified -- so a login is a record that
    GitHub did name that installation to this deployment, and the backfill
    reads it as the confirmation it was. A row without one was never verified
    by anything and stays a claim, which is the honest reading rather than a
    downgrade.

    Applied migration by migration rather than through `apply_all_migrations`,
    because the subject is what 016 does to rows that predate it -- and rows
    that predate it can only be written while 016 has not run.
    """
    connection = await asyncpg.connect(postgres_dsn)

    try:
        await reset_schema(connection)

        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path == THIS_MIGRATION:
                continue

            async with connection.transaction():
                await apply_migration(connection, path, migrations_dir=MIGRATIONS_DIR)

        await seed(connection)
        await connection.execute(
            INSERT_CLAIM_SQL, BOOTSTRAP_WORKSPACE_ID, INSTALLATION_ID, MEMBER_ID
        )
        await connection.execute(
            INSERT_CLAIM_SQL, OTHER_WORKSPACE_ID, 9999, OUTSIDER_ID
        )
        await connection.execute(
            "UPDATE github_installations SET account_login = 'acme' WHERE workspace_id = $1",
            BOOTSTRAP_WORKSPACE_ID,
        )

        async with connection.transaction():
            await apply_migration(
                connection, THIS_MIGRATION, migrations_dir=MIGRATIONS_DIR
            )

        # The row a webhook had already named is confirmed, and dated by the
        # delivery that named it rather than by the migration.
        confirmed = await connection.fetchrow(
            """
            SELECT confirmed_at, updated_at
            FROM github_installations
            WHERE workspace_id = $1
            """,
            BOOTSTRAP_WORKSPACE_ID,
        )

        assert confirmed["confirmed_at"] == confirmed["updated_at"]

        # The row nothing ever verified stays a claim.
        assert await connection.fetchval(CONFIRMED_AT_SQL, OTHER_WORKSPACE_ID) is None
    finally:
        await reset_schema(connection)
        await connection.close()
