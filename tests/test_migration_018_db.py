"""Migration 018 applied by the real runner, then asked what it built.

018 stores a choice a browser makes. An admin picks a channel out of a list and
the id travels back as a string -- which means it travels back from whoever
controls that browser, in whatever form they like. A picker is a suggestion,
never a constraint, so the question this file exists to answer is what happens
when workspace A submits workspace B's channel id.

The answer has to be the database, not the service. A resolver that scopes its
lookup correctly is right until someone writes a second one, and the second one
is where this class of bug lives. So the assertions below are mostly about rows
PostgreSQL will not store:

  * workspace A choosing workspace B's channel as its default -- refused by
    there being no column for a second workspace to go in;
  * a channel, a settings row or a preference belonging to a workspace with no
    Slack installation at all;
  * a channel id with a name and no id, or an id and no name -- the two
    spellings of one fact disagreeing;
  * an event outside the vocabulary;
  * a disconnect that discards a workspace's channels without saying so.

And three that are about the shape being useful rather than safe: the same
channel id in two workspaces is two rows (Slack ids are only unique within one
Slack workspace, and a shared key would have made two tenants collide), a
rename updates the row rather than creating a second one, and a channel that
stops being listed can be marked inaccessible while the settings row still
points at it.

Marked `db`: deselected by default, skipped when Docker is unreachable.
"""

from uuid import UUID

import asyncpg
import pytest

from tests.conftest import apply_all_migrations, reset_schema


pytestmark = pytest.mark.db

# The tenant 002 seeds, named there as literals precisely so a test can assert
# against a constant instead of querying for the value it is about to check.
BOOTSTRAP_WORKSPACE_ID = UUID("00000000-0000-7000-8000-000000000001")

# The other tenant: a real workspace, with a real installation and a real
# channel, whose rows every cross-tenant assertion below tries and fails to
# reach. It has to be real -- a nonexistent id would be refused by any spelling
# of these constraints and would prove nothing about which one is in force.
OTHER_WORKSPACE_ID = UUID("00000000-0000-7000-8000-0000000000a1")

# A third workspace that exists and has NOT connected Slack, so the tests about
# the installation key are about a missing installation rather than a missing
# workspace.
UNCONNECTED_WORKSPACE_ID = UUID("00000000-0000-7000-8000-0000000000a3")

MEMBER_ID = UUID("00000000-0000-7000-8000-0000000000e1")
OUTSIDER_ID = UUID("00000000-0000-7000-8000-0000000000e2")

BOOTSTRAP_CHANNEL_ID = "C0ENGINEER"
OTHER_CHANNEL_ID = "C0THEIRSECRET"

INSERT_USER_SQL = """
INSERT INTO users (id, email, password_hash)
VALUES (
    $1::uuid,
    'user-' || $1::text || '@example.test',
    '$argon2id$not-a-real-hash'
)
"""

# Every column 014 requires. The token columns are filled with a placeholder
# rather than anything token-shaped: nothing here presents a credential to
# Slack, and a fixture holding a realistic one is a fixture somebody copies.
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
VALUES ($1, $2, $3, 'U0BOTBOT', ARRAY['channels:read', 'chat:write'], 'database', 'placeholder', $4)
"""

INSERT_CHANNEL_SQL = """
INSERT INTO slack_channels (workspace_id, channel_id, name)
VALUES ($1, $2, $3)
"""

INSERT_SETTINGS_SQL = """
INSERT INTO slack_notification_settings
    (workspace_id, default_channel_id, default_channel_name)
VALUES ($1, $2, $3)
"""

INSERT_PREFERENCE_SQL = """
INSERT INTO slack_notification_preferences (workspace_id, event, enabled)
VALUES ($1, $2, $3)
"""


@pytest.fixture
async def connection(postgres_dsn):
    """A migrated database with two connected tenants and one unconnected."""
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
    """Two workspaces with a Slack installation and a channel each.

    Symmetric on purpose. Every cross-tenant assertion below is "workspace A's
    row pointing at workspace B's row", and a lopsided fixture -- where one
    side lacks the thing the other is reaching for -- would pass those
    assertions for the wrong reason.
    """
    for workspace_id, slug, name in (
        (OTHER_WORKSPACE_ID, "other", "Other"),
        (UNCONNECTED_WORKSPACE_ID, "unconnected", "Unconnected"),
    ):
        await connection.execute(
            "INSERT INTO workspaces (id, slug, name) VALUES ($1, $2, $3)",
            workspace_id,
            slug,
            name,
        )

    for user_id in (MEMBER_ID, OUTSIDER_ID):
        await connection.execute(INSERT_USER_SQL, user_id)

    for workspace_id, user_id, team_id, team_name in (
        (BOOTSTRAP_WORKSPACE_ID, MEMBER_ID, "T0OURS", "Vector HQ"),
        (OTHER_WORKSPACE_ID, OUTSIDER_ID, "T0THEIRS", "Other HQ"),
    ):
        await connection.execute(
            "INSERT INTO workspace_members (workspace_id, user_id, role) "
            "VALUES ($1, $2, 'admin')",
            workspace_id,
            user_id,
        )
        await connection.execute(
            INSERT_INSTALLATION_SQL, workspace_id, team_id, team_name, user_id
        )

    for workspace_id, channel_id, name in (
        (BOOTSTRAP_WORKSPACE_ID, BOOTSTRAP_CHANNEL_ID, "engineering"),
        (OTHER_WORKSPACE_ID, OTHER_CHANNEL_ID, "board-private"),
    ):
        await connection.execute(INSERT_CHANNEL_SQL, workspace_id, channel_id, name)


# --- the ordinary path works ------------------------------------------


async def test_a_workspace_can_choose_a_channel_of_its_own(connection):
    """The feature, before the refusals: this is the row the whole file is
    about protecting, and it has to be storable or the rest proves nothing."""
    await connection.execute(
        INSERT_SETTINGS_SQL,
        BOOTSTRAP_WORKSPACE_ID,
        BOOTSTRAP_CHANNEL_ID,
        "engineering",
    )

    assert (
        await connection.fetchval(
            "SELECT default_channel_name FROM slack_notification_settings "
            "WHERE workspace_id = $1",
            BOOTSTRAP_WORKSPACE_ID,
        )
        == "engineering"
    )


async def test_a_workspace_can_choose_nothing_at_all(connection):
    """The nullable half of the composite key.

    MATCH SIMPLE skips the foreign key when any column of it is NULL, which is
    what lets "connected, but nobody has picked a channel" be an ordinary row
    rather than needing a sentinel channel to point at.
    """
    await connection.execute(INSERT_SETTINGS_SQL, BOOTSTRAP_WORKSPACE_ID, None, None)

    assert (
        await connection.fetchval(
            "SELECT count(*) FROM slack_notification_settings WHERE workspace_id = $1",
            BOOTSTRAP_WORKSPACE_ID,
        )
        == 1
    )


async def test_the_same_slack_channel_id_in_two_workspaces_is_two_rows(connection):
    """A Slack channel id is unique inside ONE Slack workspace and nowhere else.

    Two Vector workspaces connected to two different Slacks can genuinely hold
    the same id, so a primary key on `channel_id` alone would make the second
    tenant's sync fail with a unique violation -- an outage in workspace B
    caused by whatever workspace A happened to name a channel.
    """
    await connection.execute(
        INSERT_CHANNEL_SQL, OTHER_WORKSPACE_ID, BOOTSTRAP_CHANNEL_ID, "engineering"
    )

    assert (
        await connection.fetchval(
            "SELECT count(*) FROM slack_channels WHERE channel_id = $1",
            BOOTSTRAP_CHANNEL_ID,
        )
        == 2
    )


async def test_a_rename_updates_the_row_rather_than_creating_a_second(connection):
    """The id is the key, which is the reason it is what gets stored.

    A key built on the name would make "#engineering renamed to #eng" two rows,
    and a settings row pointing at the first would go on naming a channel that
    no longer exists under that name.
    """
    await connection.execute(
        """
        INSERT INTO slack_channels (workspace_id, channel_id, name)
        VALUES ($1, $2, $3)
        ON CONFLICT (workspace_id, channel_id) DO UPDATE SET name = EXCLUDED.name
        """,
        BOOTSTRAP_WORKSPACE_ID,
        BOOTSTRAP_CHANNEL_ID,
        "eng",
    )

    rows = await connection.fetch(
        "SELECT name FROM slack_channels WHERE workspace_id = $1",
        BOOTSTRAP_WORKSPACE_ID,
    )

    assert [row["name"] for row in rows] == ["eng"]


# --- the cross-tenant refusals ----------------------------------------


async def test_a_workspace_cannot_choose_another_workspaces_channel(connection):
    """The attack this migration exists for.

    An admin of the bootstrap workspace submits the other tenant's channel id
    -- guessed, leaked, or read from a screenshot. A resolver that forgot to
    scope its lookup would hand it straight to the write, and the row would be
    a live delivery route into somebody else's Slack: this workspace's issue
    titles, assignees and project names, posted into a channel it has no
    relationship with.

    One workspace_id feeds both the installation key and the channel key, so
    there is no column for the second workspace to go in. The row is refused by
    the schema rather than by a service remembering.
    """
    with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
        await connection.execute(
            INSERT_SETTINGS_SQL,
            BOOTSTRAP_WORKSPACE_ID,
            OTHER_CHANNEL_ID,
            "board-private",
        )

    assert raised.value.constraint_name == (
        "slack_notification_settings_default_channel_fk"
    )


async def test_a_settings_row_cannot_smuggle_a_second_workspace_through_its_own_id(
    connection,
):
    """The other direction: claim to BE the other workspace.

    A row whose workspace_id is the other tenant's, pointing at this tenant's
    channel. Whichever way round an attacker spells the mismatch, the one
    workspace_id column is what both halves are checked against -- so one of
    the two constraints is looking at it.
    """
    with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
        await connection.execute(
            INSERT_SETTINGS_SQL,
            OTHER_WORKSPACE_ID,
            BOOTSTRAP_CHANNEL_ID,
            "engineering",
        )

    assert raised.value.constraint_name == (
        "slack_notification_settings_default_channel_fk"
    )


@pytest.mark.parametrize(
    ("statement", "arguments", "constraint"),
    [
        (
            INSERT_CHANNEL_SQL,
            (UNCONNECTED_WORKSPACE_ID, "C0GENERAL", "general"),
            "slack_channels_installation_fk",
        ),
        (
            INSERT_SETTINGS_SQL,
            (UNCONNECTED_WORKSPACE_ID, None, None),
            "slack_notification_settings_installation_fk",
        ),
        (
            INSERT_PREFERENCE_SQL,
            (UNCONNECTED_WORKSPACE_ID, "issue_assigned", True),
            "slack_notification_preferences_installation_fk",
        ),
    ],
    ids=["channel", "settings", "preference"],
)
async def test_nothing_hangs_off_a_workspace_with_no_installation(
    connection, statement, arguments, constraint
):
    """Every table here references `slack_installations`, not `workspaces`.

    The distinction is what makes "channels this workspace's bot can see" a
    statement the schema enforces: with no installation there is no bot token,
    so nothing could have listed a channel, and the row would be a claim about
    an API call that never happened. The workspace itself exists, so this is
    about the installation key and not about a missing tenant.
    """
    with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
        await connection.execute(statement, *arguments)

    assert raised.value.constraint_name == constraint


# --- the shapes that must not be storable -----------------------------


@pytest.mark.parametrize(
    ("channel_id", "channel_name"),
    [(BOOTSTRAP_CHANNEL_ID, None), (None, "engineering")],
    ids=["an id with no name", "a name with no id"],
)
async def test_half_a_channel_choice_is_refused(connection, channel_id, channel_name):
    """The two columns are one fact and must agree about whether it is set.

    An id with no name shows an admin a raw "C0ENGINEER" on a settings screen.
    A name with no id is worse: it reads to a human as a configured integration
    and posts nowhere at all.
    """
    with pytest.raises(asyncpg.CheckViolationError) as raised:
        await connection.execute(
            INSERT_SETTINGS_SQL, BOOTSTRAP_WORKSPACE_ID, channel_id, channel_name
        )

    assert raised.value.constraint_name == ("slack_notification_settings_channel_pair")


async def test_an_invented_notification_event_is_refused(connection):
    """Six events exist because six things are worth interrupting a team for.

    A seventh is a typo in a writer, and a typo that reaches the table is a
    preference row nothing will ever match -- a toggle that silently does
    nothing, forever, with no query able to find it.
    """
    with pytest.raises(asyncpg.CheckViolationError) as raised:
        await connection.execute(
            INSERT_PREFERENCE_SQL, BOOTSTRAP_WORKSPACE_ID, "issue_asigned", True
        )

    assert raised.value.constraint_name == (
        "slack_notification_preferences_event_known"
    )


async def test_every_event_the_application_knows_is_one_this_schema_accepts(connection):
    """The other side of the CHECK, and the half that catches a drift.

    The negative test above passes for a schema whose vocabulary is empty. This
    one walks the application's own tuple, so an event added to
    app/domain/slack.py without a migration fails here rather than as a
    constraint violation the first time an admin toggles it.
    """
    from app.domain.slack import SLACK_NOTIFICATION_EVENTS

    for event in SLACK_NOTIFICATION_EVENTS:
        await connection.execute(
            INSERT_PREFERENCE_SQL, BOOTSTRAP_WORKSPACE_ID, event, True
        )

    assert await connection.fetchval(
        "SELECT count(*) FROM slack_notification_preferences WHERE workspace_id = $1",
        BOOTSTRAP_WORKSPACE_ID,
    ) == len(SLACK_NOTIFICATION_EVENTS)


@pytest.mark.parametrize(
    "channel_id",
    ["", "c0lowercase", "C", "with space", "C0" + "X" * 63],
    ids=["empty", "lowercase", "too short", "a space", "too long"],
)
async def test_a_channel_id_that_is_not_one_slack_issues_is_refused(
    connection, channel_id
):
    """A bound and a shape, not a claim the channel exists.

    The empty string is the case that matters most: it is what an absent key
    read as '' looks like, and an empty channel id stored as a default would be
    a workspace configured to post to nothing. The bound is the other: an
    unbounded TEXT used as half of a primary key is a way to make an index
    enormous with one crafted response.
    """
    with pytest.raises(asyncpg.CheckViolationError) as raised:
        await connection.execute(
            INSERT_CHANNEL_SQL, BOOTSTRAP_WORKSPACE_ID, channel_id, "general"
        )

    assert raised.value.constraint_name == "slack_channels_channel_id_format"


async def test_a_channel_name_longer_than_slack_allows_is_refused(connection):
    """80 is Slack's own ceiling, so a name Slack accepts is one this accepts.

    Matching rather than guessing is the point: a tighter bound would fail a
    sync on a channel somebody legitimately created, and could only be relaxed
    by another migration.
    """
    with pytest.raises(asyncpg.CheckViolationError) as raised:
        await connection.execute(
            INSERT_CHANNEL_SQL, BOOTSTRAP_WORKSPACE_ID, "C0GENERAL", "x" * 81
        )

    assert raised.value.constraint_name == "slack_channels_name_length"


# --- what a sync and a disconnect must not silently do ----------------


async def test_a_chosen_channel_can_go_inaccessible_without_losing_the_choice(
    connection,
):
    """Why the sync marks instead of deleting.

    A channel drops out of `conversations.list` when it is deleted, made
    private, or the bot loses sight of it -- and one of those may be the
    channel an admin chose. The RESTRICT below would abort the whole sync on a
    delete. Marking keeps the row, keeps the name the settings screen needs in
    order to explain itself, and keeps the choice intact so it works again the
    moment the bot is re-invited.
    """
    await connection.execute(
        INSERT_SETTINGS_SQL,
        BOOTSTRAP_WORKSPACE_ID,
        BOOTSTRAP_CHANNEL_ID,
        "engineering",
    )
    await connection.execute(
        "UPDATE slack_channels SET is_accessible = FALSE "
        "WHERE workspace_id = $1 AND channel_id = $2",
        BOOTSTRAP_WORKSPACE_ID,
        BOOTSTRAP_CHANNEL_ID,
    )

    assert (
        await connection.fetchval(
            "SELECT default_channel_name FROM slack_notification_settings "
            "WHERE workspace_id = $1",
            BOOTSTRAP_WORKSPACE_ID,
        )
        == "engineering"
    )


async def test_deleting_a_chosen_channel_is_refused_rather_than_clearing_the_choice(
    connection,
):
    """RESTRICT, not CASCADE.

    A cascade here would silently discard an admin's choice during a routine
    sync, leaving notifications that stopped with nothing anywhere to say why.

    `RestrictViolationError`, not `ForeignKeyViolationError`. The two are
    siblings under IntegrityConstraintViolationError rather than one being the
    other's parent, and PostgreSQL raises them for opposite situations: the
    foreign-key error means a child pointed at a parent that was not there,
    while this one means the parent was there and declined to leave. Catching
    the wrong sibling would pass for a schema with no constraint at all, since
    the delete would then simply succeed and raise nothing.
    """
    await connection.execute(
        INSERT_SETTINGS_SQL,
        BOOTSTRAP_WORKSPACE_ID,
        BOOTSTRAP_CHANNEL_ID,
        "engineering",
    )

    with pytest.raises(asyncpg.RestrictViolationError):
        await connection.execute(
            "DELETE FROM slack_channels WHERE workspace_id = $1 AND channel_id = $2",
            BOOTSTRAP_WORKSPACE_ID,
            BOOTSTRAP_CHANNEL_ID,
        )


@pytest.mark.parametrize(
    ("statement", "arguments"),
    [
        (INSERT_CHANNEL_SQL, (BOOTSTRAP_WORKSPACE_ID, "C0GENERAL", "general")),
        (
            INSERT_PREFERENCE_SQL,
            (BOOTSTRAP_WORKSPACE_ID, "issue_assigned", True),
        ),
    ],
    ids=["a channel", "a preference"],
)
async def test_disconnecting_does_not_silently_discard_what_hangs_off_it(
    connection, statement, arguments
):
    """A one-line delete must not discard rows in three other tables while
    reporting `DELETE 1`.

    SlackService.disconnect removes them itself, in dependency order, in the
    same transaction -- which is a decision made out loud in code rather than a
    side effect of a cascade nobody reads. This is what makes forgetting one of
    those tables a loud failure instead of an orphaned row.
    """
    await connection.execute(statement, *arguments)

    with pytest.raises(asyncpg.RestrictViolationError):
        await connection.execute(
            "DELETE FROM slack_installations WHERE workspace_id = $1",
            BOOTSTRAP_WORKSPACE_ID,
        )


async def test_the_documented_teardown_order_actually_works(connection):
    """The order SlackRepository.delete_dependents uses, run against the real
    constraints.

    Every foreign key in 018 is RESTRICT, so the order is not a preference: it
    is the only order that completes. Settings first (the one table pointing at
    `slack_channels`), preferences, channels, then the installation. Asserting
    it here means a reordering in the repository is caught by a test that says
    what it broke.
    """
    await connection.execute(
        INSERT_SETTINGS_SQL,
        BOOTSTRAP_WORKSPACE_ID,
        BOOTSTRAP_CHANNEL_ID,
        "engineering",
    )
    await connection.execute(
        INSERT_PREFERENCE_SQL, BOOTSTRAP_WORKSPACE_ID, "issue_assigned", True
    )

    for statement in (
        "DELETE FROM slack_notification_settings WHERE workspace_id = $1",
        "DELETE FROM slack_notification_preferences WHERE workspace_id = $1",
        "DELETE FROM slack_channels WHERE workspace_id = $1",
        "DELETE FROM slack_installations WHERE workspace_id = $1",
    ):
        await connection.execute(statement, BOOTSTRAP_WORKSPACE_ID)

    assert (
        await connection.fetchval(
            "SELECT count(*) FROM slack_installations WHERE workspace_id = $1",
            BOOTSTRAP_WORKSPACE_ID,
        )
        == 0
    )
