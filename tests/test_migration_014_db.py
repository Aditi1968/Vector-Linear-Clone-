"""Migration 014 applied by the real runner, then asked what it refuses.

Three of this migration's decisions are one word away from a version that
reads correctly in a diff and is wrong in production, and none of them is
visible in a catalog listing that does not print a key's columns:

  * `connected_by_user_id UUID REFERENCES users (id)` is the natural spelling
    and it records any account in the system as the admin who connected any
    workspace's Slack. The reference has to be the composite
    `(workspace_id, connected_by_user_id) -> workspace_members (workspace_id,
    user_id)`, which is what makes a non-member connector a row PostgreSQL
    will not store;
  * without `slack_installations_team_key`, two Vector workspaces can hold the
    same Slack team id -- and an inbound event, which carries only that id,
    then routes to whichever row the planner reached first;
  * without a primary key on `slack_event_deliveries.event_id`, Slack's
    retries are ordinary events and every delivery is processed again.

So this file writes rows to find out what the schema actually refuses, and
then drives the real service over a real pool to check that what the schema
refuses is reported as the expected outcome the product describes rather than
as an unhandled asyncpg error.

Marked `db`: deselected by default, skipped when Docker is unreachable.
"""

from uuid import UUID

import asyncpg
import pytest

from app.domain.slack import SlackTeamAlreadyConnectedError
from app.domain.tenancy import AuthorizedWorkspaceScope
from app.repositories.slack import SlackRepository
from app.services.slack import DatabaseTokenStore, SlackGrant, SlackService

from tests.conftest import apply_all_migrations, reset_schema


pytestmark = pytest.mark.db


# The tenant 002 seeds, named there as literals precisely so a test can assert
# against a constant rather than querying for the value it is about to check.
BOOTSTRAP_WORKSPACE_ID = UUID("00000000-0000-7000-8000-000000000001")

OTHER_WORKSPACE_ID = UUID("00000000-0000-7000-8000-0000000000a1")

# A member of the bootstrap workspace: the one account that may be recorded as
# having connected its Slack.
MEMBER_ID = UUID("00000000-0000-7000-8000-0000000000e1")

# A real account with a real membership -- of the OTHER workspace. This is the
# id the naive `REFERENCES users (id)` would accept as the connector of the
# bootstrap workspace's installation, and the composite key refuses. It has to
# be a real user, because a nonexistent id would be refused by either spelling
# and would prove nothing about which one is in force.
OUTSIDER_ID = UUID("00000000-0000-7000-8000-0000000000e2")

SLACK_TEAM_ID = "T024BE7LD"
BOT_USER_ID = "U0BOTBOT"
BOT_TOKEN = "xoxb-not-a-real-token"

INSERT_USER_SQL = """
INSERT INTO users (id, email, password_hash)
VALUES (
    $1::uuid,
    'user-' || $1::text || '@example.test',
    '$argon2id$not-a-real-hash'
)
"""

INSERT_INSTALLATION_SQL = """
INSERT INTO slack_installations (
    workspace_id,
    slack_team_id,
    slack_team_name,
    bot_user_id,
    scopes,
    bot_token_backend,
    bot_token_reference,
    connected_by_user_id
)
VALUES ($1, $2, 'Vector HQ', $3, ARRAY['chat:write'], 'database', $4, $5)
"""


def grant(*, team_id=SLACK_TEAM_ID):
    return SlackGrant(
        slack_team_id=team_id,
        slack_team_name="Vector HQ",
        bot_user_id=BOT_USER_ID,
        scopes=("channels:read", "chat:write"),
        bot_token=BOT_TOKEN,
    )


def scope(workspace_id=BOOTSTRAP_WORKSPACE_ID, user_id=MEMBER_ID, role="admin"):
    return AuthorizedWorkspaceScope(
        workspace_id=workspace_id,
        user_id=user_id,
        role=role,
    )


@pytest.fixture
async def applied(postgres_dsn):
    """The whole migration chain, then two tenants and two accounts.

    Every table is dropped first because the container is shared for the whole
    session, so a `workspaces` left behind by another file would make 002's
    CREATE TABLE fail here for reasons unrelated to this migration.

    Applied through `scripts.apply_migration`, exactly as an operator would,
    rather than by executing the file's text: a hand-run gets no ledger row
    and no advisory lock, and this file is partly about the runner accepting
    014 at all.
    """
    connection = await asyncpg.connect(postgres_dsn)

    try:
        await reset_schema(connection)
        await apply_all_migrations(connection)

        await connection.execute(
            "INSERT INTO workspaces (id, slug, name) VALUES ($1, $2, $3)",
            OTHER_WORKSPACE_ID,
            "acme",
            "Acme",
        )

        for user_id in (MEMBER_ID, OUTSIDER_ID):
            await connection.execute(INSERT_USER_SQL, user_id)

        await connection.execute(
            "INSERT INTO workspace_members (workspace_id, user_id, role) "
            "VALUES ($1, $2, 'admin')",
            BOOTSTRAP_WORKSPACE_ID,
            MEMBER_ID,
        )
        await connection.execute(
            "INSERT INTO workspace_members (workspace_id, user_id, role) "
            "VALUES ($1, $2, 'admin')",
            OTHER_WORKSPACE_ID,
            OUTSIDER_ID,
        )

        yield connection
    finally:
        await connection.close()


@pytest.fixture
async def pool(applied, postgres_dsn):
    """A real pool over the migrated database, for the service-level tests.

    Depends on `applied` so the schema and the seed rows exist before the pool
    is opened; the service owns transactions, so it needs a pool rather than
    the fixture's single connection.
    """
    created = await asyncpg.create_pool(postgres_dsn, min_size=1, max_size=2)

    try:
        yield created
    finally:
        await created.close()


def service(pool, *, configured=True):
    return SlackService(
        pool=pool,
        repository=SlackRepository(),
        token_store=DatabaseTokenStore(),
        configured=configured,
    )


# --- the schema exists ------------------------------------------------


async def test_the_runner_applies_014_and_records_it(applied):
    versions = await applied.fetch("SELECT version FROM schema_migrations")

    assert "014" in {row["version"] for row in versions}


async def test_both_tables_exist(applied):
    tables = await applied.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
    )
    names = {row["tablename"] for row in tables}

    assert {"slack_installations", "slack_event_deliveries"} <= names


# --- what the installation table refuses -------------------------------


async def test_a_connector_from_another_workspace_is_refused(applied):
    """The composite foreign key, doing the job the single-column one cannot.

    OUTSIDER_ID is a real account with a real membership -- of the other
    workspace. `REFERENCES users (id)` would accept it here without complaint;
    the pair `(workspace_id, connected_by_user_id)` has nowhere to put the
    second workspace, so the row cannot exist.
    """
    with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
        await applied.execute(
            INSERT_INSTALLATION_SQL,
            BOOTSTRAP_WORKSPACE_ID,
            SLACK_TEAM_ID,
            BOT_USER_ID,
            BOT_TOKEN,
            OUTSIDER_ID,
        )

    assert raised.value.constraint_name == "slack_installations_connected_by_fk"


async def test_one_slack_workspace_cannot_be_connected_twice(applied):
    """The routing guarantee: an event's team id resolves to one tenant.

    Two DIFFERENT Vector workspaces, one Slack team id. Without the unique
    constraint both rows exist and `find_by_team` returns whichever the
    planner reached first -- delivering one tenant's Slack traffic into
    another tenant's data.
    """
    await applied.execute(
        INSERT_INSTALLATION_SQL,
        BOOTSTRAP_WORKSPACE_ID,
        SLACK_TEAM_ID,
        BOT_USER_ID,
        BOT_TOKEN,
        MEMBER_ID,
    )

    with pytest.raises(asyncpg.UniqueViolationError) as raised:
        await applied.execute(
            INSERT_INSTALLATION_SQL,
            OTHER_WORKSPACE_ID,
            SLACK_TEAM_ID,
            BOT_USER_ID,
            BOT_TOKEN,
            OUTSIDER_ID,
        )

    assert raised.value.constraint_name == "slack_installations_team_key"


async def test_a_workspace_holding_an_installation_cannot_be_deleted(applied):
    """RESTRICT, not CASCADE.

    A cascade here would drop a bot token as a third-order effect of a delete
    against another table, leaving a credential live at Slack that Vector no
    longer knows it holds -- and reporting `DELETE 1`.
    """
    await applied.execute(
        INSERT_INSTALLATION_SQL,
        BOOTSTRAP_WORKSPACE_ID,
        SLACK_TEAM_ID,
        BOT_USER_ID,
        BOT_TOKEN,
        MEMBER_ID,
    )

    # RestrictViolationError, not ForeignKeyViolationError: PostgreSQL
    # reports a RESTRICT refusal under its own SQLSTATE, and asyncpg maps the
    # two to sibling classes rather than one to the other.
    with pytest.raises(asyncpg.RestrictViolationError):
        await applied.execute(
            "DELETE FROM workspaces WHERE id = $1",
            BOOTSTRAP_WORKSPACE_ID,
        )


async def test_the_connectors_membership_cannot_be_removed_while_recorded(applied):
    """Removing the admin who connected Slack is refused, not rewritten.

    The alternative -- ON DELETE SET NULL -- would silently vacate the audit
    trail as a side effect of a membership change. The caller disconnects
    first, which is a decision someone should make out loud.
    """
    await applied.execute(
        INSERT_INSTALLATION_SQL,
        BOOTSTRAP_WORKSPACE_ID,
        SLACK_TEAM_ID,
        BOT_USER_ID,
        BOT_TOKEN,
        MEMBER_ID,
    )

    with pytest.raises(asyncpg.RestrictViolationError):
        await applied.execute(
            "DELETE FROM workspace_members WHERE workspace_id = $1 AND user_id = $2",
            BOOTSTRAP_WORKSPACE_ID,
            MEMBER_ID,
        )


# --- the deduplication ledger ------------------------------------------


async def test_one_event_id_can_only_be_recorded_once(applied):
    await applied.execute(
        "INSERT INTO slack_event_deliveries (event_id) VALUES ($1)", "Ev0001"
    )

    with pytest.raises(asyncpg.UniqueViolationError):
        await applied.execute(
            "INSERT INTO slack_event_deliveries (event_id) VALUES ($1)", "Ev0001"
        )


async def test_a_redelivered_event_is_claimed_by_the_first_caller_only(pool):
    """The property Slack's retries make non-optional, against a real server.

    Both calls succeed -- the second is not an error -- and exactly one of
    them reports having claimed the event. This is what a future ingestion
    step will hang off, so "processed once" reduces to "claimed once".
    """
    slack = service(pool)

    assert await slack.claim_event(event_id="Ev0001") is True
    assert await slack.claim_event(event_id="Ev0001") is False

    # A different id is unaffected: the ledger deduplicates deliveries, not
    # events in general.
    assert await slack.claim_event(event_id="Ev0002") is True


async def test_concurrent_claims_of_one_event_produce_exactly_one_winner(pool):
    """Two deliveries in flight at once, which is what a retry actually is.

    A SELECT-then-INSERT passes the sequential test above and fails this one:
    both readers find nothing and both insert. The claim is one statement, so
    the decision happens inside the index write and the loser sees the row.
    """
    import asyncio

    slack = service(pool)

    results = await asyncio.gather(
        slack.claim_event(event_id="Ev0003"),
        slack.claim_event(event_id="Ev0003"),
    )

    assert sorted(results) == [False, True]


# --- the service against a real database -------------------------------


async def test_connect_then_status_reports_the_team_and_granted_scopes(pool):
    slack = service(pool)

    await slack.connect(scope=scope(), grant=grant())

    view = await slack.status(scope=scope())

    assert view.status == "connected"
    assert view.team_name == "Vector HQ"
    # TEXT[] round-trips as the tuple the entity declares, in order.
    assert view.scopes == ("channels:read", "chat:write")


async def test_reconnecting_replaces_the_installation_rather_than_adding_one(pool):
    """One workspace, one installation, whatever the reconnect says.

    A second row would mean a superseded bot token nothing in the product can
    reach or revoke; the primary key on `workspace_id` is what makes that
    impossible, and the upsert is what makes reconnecting still work.
    """
    slack = service(pool)

    await slack.connect(scope=scope(), grant=grant())
    await slack.connect(
        scope=scope(),
        grant=SlackGrant(
            slack_team_id=SLACK_TEAM_ID,
            slack_team_name="Vector HQ (renamed)",
            bot_user_id=BOT_USER_ID,
            scopes=("chat:write",),
            bot_token="xoxb-a-newer-token",
        ),
    )

    async with pool.acquire() as connection:
        rows = await connection.fetch(
            "SELECT slack_team_name, scopes FROM slack_installations "
            "WHERE workspace_id = $1",
            BOOTSTRAP_WORKSPACE_ID,
        )

    assert len(rows) == 1
    assert rows[0]["slack_team_name"] == "Vector HQ (renamed)"
    assert rows[0]["scopes"] == ["chat:write"]


async def test_a_slack_workspace_taken_by_another_tenant_is_an_expected_error(pool):
    """The constraint violation, translated where the product can act on it.

    Raised as SlackTeamAlreadyConnectedError rather than escaping as an
    asyncpg failure, and the message names neither Vector workspace -- telling
    the admin which one holds it would answer, for anyone who can reach a
    connect button, which workspaces exist here.
    """
    slack = service(pool)

    await slack.connect(scope=scope(), grant=grant())

    with pytest.raises(SlackTeamAlreadyConnectedError):
        await slack.connect(
            scope=scope(workspace_id=OTHER_WORKSPACE_ID, user_id=OUTSIDER_ID),
            grant=grant(),
        )


async def test_the_token_is_reachable_only_through_the_store(pool):
    """The round trip the abstraction exists for, end to end.

    The token goes in through `store`, the row holds the reference and the
    backend that issued it, and it comes back out through `fetch`. Nothing in
    between -- not the entity, not the view, not the status query -- carries
    it, which is asserted by the entity having no field for one.
    """
    slack = service(pool)

    await slack.connect(scope=scope(), grant=grant())

    assert await slack.bot_token(scope=scope()) == BOT_TOKEN

    async with pool.acquire() as connection:
        stored = await connection.fetchrow(
            "SELECT bot_token_backend FROM slack_installations WHERE workspace_id = $1",
            BOOTSTRAP_WORKSPACE_ID,
        )

    assert stored["bot_token_backend"] == "database"


async def test_disconnect_removes_the_row_and_is_idempotent(pool):
    slack = service(pool)

    await slack.connect(scope=scope(), grant=grant())

    assert (await slack.disconnect(scope=scope())).status == "disconnected"
    # Pressing the button twice is an ordinary race, not an error.
    assert (await slack.disconnect(scope=scope())).status == "disconnected"

    async with pool.acquire() as connection:
        remaining = await connection.fetchval(
            "SELECT count(*) FROM slack_installations WHERE workspace_id = $1",
            BOOTSTRAP_WORKSPACE_ID,
        )

    assert remaining == 0


async def test_an_event_routes_to_the_workspace_that_installed_it(pool):
    """The webhook path's only lookup, against a real unique index.

    Takes no scope, because on this path there is no viewer -- the evidence is
    the verified request signature the transport checked first.
    """
    slack = service(pool)

    await slack.connect(scope=scope(), grant=grant())

    found = await slack.installation_for_team(slack_team_id=SLACK_TEAM_ID)

    assert found is not None
    assert found.workspace_id == BOOTSTRAP_WORKSPACE_ID
    assert found.bot_user_id == BOT_USER_ID

    # A team nobody installed is an ordinary absent row, not an error: Slack
    # delivers events for workspaces this deployment has disconnected.
    assert await slack.installation_for_team(slack_team_id="TNOSUCH") is None
