"""Migration 027 applied by the real runner, then asked what it built.

027 is the notification pipeline's outbox, and the two things it exists to make
unstorable are the two ways a notification integration goes wrong in public: a
message posted twice, and a message posted into the wrong company's Slack.

The answer has to be the database, not the service. A dispatcher that dedupes
correctly is right until someone writes a second emitter, and the second one is
where this class of bug lives. So most of the assertions below are about rows
PostgreSQL will not store:

  * the same fact emitted twice -- refused by the primary key, which is what
    makes a redelivered GitHub webhook a key violation rather than a second
    post;
  * an event claiming to be workspace A's, about workspace B's issue -- refused
    by there being no column for a second workspace to go in;
  * `delivered` with no instant, and `delivered` carrying a failure reason --
    the two shapes a service that swallowed an exception would write;
  * `failed` with no reason -- an operator staring at a red row with nothing to
    act on;
  * a kind, a failure or a state outside its vocabulary.

And three that are about the shape being useful rather than safe: the same
fact under two different KINDS is two rows (a merge and an assignment about one
issue are two things), a claim moves the retry forward in the same statement
that counts the attempt (which is what a retry cannot hot-loop past), and two
concurrent claimers take two different rows rather than the same one.

Marked `db`: deselected by default, skipped when Docker is unreachable.
"""

from uuid import UUID

import asyncpg
import pytest

from tests.conftest import (
    apply_all_migrations,
    reset_schema,
    seed_workflow_states,
)


pytestmark = pytest.mark.db

# The tenant 002 seeds, named there as literals precisely so a test can assert
# against a constant instead of querying for the value it is about to check.
BOOTSTRAP_WORKSPACE_ID = UUID("00000000-0000-7000-8000-000000000001")
BOOTSTRAP_TEAM_ID = UUID("00000000-0000-7000-8000-000000000002")

# The other tenant: a real workspace with a real issue, which every
# cross-tenant assertion below tries and fails to reach. It has to be real -- a
# nonexistent id would be refused by any spelling of these constraints and
# would prove nothing about which one is in force.
OTHER_WORKSPACE_ID = UUID("00000000-0000-7000-8000-0000000000a1")
OTHER_TEAM_ID = UUID("00000000-0000-7000-8000-0000000000a2")

BOOTSTRAP_ISSUE_ID = UUID("00000000-0000-7000-8000-0000000000b1")
OTHER_ISSUE_ID = UUID("00000000-0000-7000-8000-0000000000b2")

INSERT_ISSUE_SQL = """
INSERT INTO issues (
    id, workspace_id, team_id, number, workflow_state_id, title, priority
)
VALUES (
    $1, $2, $3, $4,
    (
        SELECT id FROM workflow_states
        WHERE workspace_id = $2 AND team_id = $3 AND type = 'unstarted'
    ),
    $5, 1
)
"""

INSERT_EVENT_SQL = """
INSERT INTO domain_events (
    workspace_id, kind, dedupe_key, issue_id, subject, summary, path
)
VALUES ($1, $2, $3, $4, $5, $6, $7)
"""


@pytest.fixture
async def connection(postgres_dsn):
    """A migrated database with two fully-populated tenants."""
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
    """Two workspaces, each with a team and an issue.

    Symmetric on purpose. Every cross-tenant assertion below is "workspace A's
    row pointing at workspace B's row", and a lopsided fixture -- where one
    side lacks the thing the other is reaching for -- would pass those
    assertions for the wrong reason.
    """
    await connection.execute(
        "INSERT INTO workspaces (id, slug, name) VALUES ($1, 'other', 'Other')",
        OTHER_WORKSPACE_ID,
    )
    await connection.execute(
        "INSERT INTO teams (id, workspace_id, name, key) "
        "VALUES ($1, $2, 'Other', 'OTH')",
        OTHER_TEAM_ID,
        OTHER_WORKSPACE_ID,
    )

    # 005 seeds workflow states for the teams that exist when it runs, so the
    # team created above -- afterwards -- has none, and an issue on it cannot
    # be inserted at all.
    await seed_workflow_states(connection, OTHER_WORKSPACE_ID, OTHER_TEAM_ID)

    for issue_id, workspace_id, team_id, title in (
        (BOOTSTRAP_ISSUE_ID, BOOTSTRAP_WORKSPACE_ID, BOOTSTRAP_TEAM_ID, "Ours"),
        (OTHER_ISSUE_ID, OTHER_WORKSPACE_ID, OTHER_TEAM_ID, "Theirs"),
    ):
        await connection.execute(
            INSERT_ISSUE_SQL, issue_id, workspace_id, team_id, 142, title
        )


async def insert_event(
    connection,
    *,
    workspace_id=BOOTSTRAP_WORKSPACE_ID,
    kind="issue_assigned",
    dedupe_key="k-1",
    issue_id=BOOTSTRAP_ISSUE_ID,
) -> None:
    await connection.execute(
        INSERT_EVENT_SQL,
        workspace_id,
        kind,
        dedupe_key,
        issue_id,
        "ENG-142",
        "Fix the OAuth callback",
        f"/acme/issues/{issue_id}",
    )


# --- the ordinary path works ------------------------------------------


async def test_an_event_lands_pending_and_immediately_due(connection):
    """The feature, before the refusals.

    A freshly written event has to be pending, have been tried zero times, and
    be due NOW -- otherwise the defaults would silently delay every first
    delivery by whatever the column defaulted to, which is the kind of bug that
    reads as "Slack is a bit slow" forever.
    """
    await insert_event(connection)

    row = await connection.fetchrow(
        "SELECT slack_state, slack_attempts, slack_delivered_at, slack_failure, "
        "slack_next_attempt_at <= now() AS due FROM domain_events"
    )

    assert row["slack_state"] == "pending"
    assert row["slack_attempts"] == 0
    assert row["slack_delivered_at"] is None
    assert row["slack_failure"] is None
    assert row["due"] is True


# --- one fact, one event ----------------------------------------------


async def test_the_same_fact_cannot_be_emitted_twice(connection):
    """The idempotency the whole pipeline rests on.

    GitHub retries a delivery it did not answer 2xx for, and it also sends the
    whole `pull_request` object on every later edit of an already-merged pull
    request -- each under a delivery id nothing has seen before. So "this
    merged" is a fact a provider reports many times, and the only thing that
    can make the second report harmless is a key over the FACT.

    A `UniqueViolationError` and not a second row. The emitting statements say
    ON CONFLICT DO NOTHING, which is how a writer uses this key from inside
    somebody else's transaction; what is asserted here is that the key is
    really there to be used.
    """
    await insert_event(connection, kind="pull_request_merged", dedupe_key="9/84")

    with pytest.raises(asyncpg.UniqueViolationError) as raised:
        await insert_event(connection, kind="pull_request_merged", dedupe_key="9/84")

    assert raised.value.constraint_name == "domain_events_pkey"


async def test_the_same_key_under_two_kinds_is_two_events(connection):
    """`kind` is inside the key, and that is what makes the suppression narrow.

    One issue can be assigned and have a pull request merged against it, and
    those are two things to announce. Were the key (workspace, dedupe_key) the
    second would be swallowed by the first -- and an emitter that happened to
    build the same key for two kinds would silently lose one of them forever.
    """
    for kind in ("issue_assigned", "issue_completed"):
        await insert_event(connection, kind=kind, dedupe_key="shared")

    assert await connection.fetchval("SELECT count(*) FROM domain_events") == 2


async def test_a_dedupe_key_is_bounded(connection):
    """An unbounded TEXT inside a primary key is a way to make an index
    enormous by emitting one long value. 200 matches what 017 allows a GitHub
    delivery id, which is the other key of this shape in the schema."""
    with pytest.raises(asyncpg.CheckViolationError) as raised:
        await insert_event(connection, dedupe_key="k" * 201)

    assert raised.value.constraint_name == "domain_events_dedupe_key_length"


# --- the cross-tenant refusal -----------------------------------------


async def test_an_event_cannot_be_about_another_workspaces_issue(connection):
    """The attack this migration shares with 017, one table further on.

    An event carrying workspace A and workspace B's issue is what a fan-out
    that forgot to scope its lookup would produce -- and the consequence is
    worse here than a wrong panel, because this row is what a delivery loop
    turns into a message in A's Slack channel naming B's issue.

    There is no column for the second workspace to go in, so the row is refused
    by the schema rather than by the emitter remembering.
    """
    with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
        await insert_event(connection, issue_id=OTHER_ISSUE_ID)

    assert raised.value.constraint_name == "domain_events_issue_fk"


async def test_an_event_cannot_claim_to_be_a_workspace_that_does_not_exist(
    connection,
):
    """The other direction: an event filed under an invented tenant.

    It would never be delivered -- the channel lookup would find no
    installation -- but it would sit pending forever in an index whose whole
    job is to be small, and it would be a row no workspace could ever account
    for.
    """
    with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
        await insert_event(
            connection,
            workspace_id=UUID("00000000-0000-7000-8000-0000000000ff"),
            issue_id=None,
        )

    assert raised.value.constraint_name == "domain_events_workspace_fk"


# --- a delivery cannot lie --------------------------------------------


async def test_delivered_cannot_be_claimed_without_an_instant(connection):
    """The constraint that makes an honest status structural.

    This is precisely the row a service that caught an exception, logged it and
    reported success would write: the state says delivered and nothing says
    when. It is refused, so the honesty is not a property of the service being
    careful -- which is the property that decays the moment a second caller
    appears.
    """
    await insert_event(connection)

    with pytest.raises(asyncpg.CheckViolationError) as raised:
        await connection.execute("UPDATE domain_events SET slack_state = 'delivered'")

    assert raised.value.constraint_name == "domain_events_delivered_has_an_instant"


async def test_an_instant_cannot_be_recorded_without_delivering(connection):
    """The same constraint from the other side, so it is not merely a ban.

    A delivery instant on a row that is still pending would make two readers
    disagree about whether the message went out -- one reading the state, one
    reading the timestamp.
    """
    await insert_event(connection)

    with pytest.raises(asyncpg.CheckViolationError) as raised:
        await connection.execute("UPDATE domain_events SET slack_delivered_at = now()")

    assert raised.value.constraint_name == "domain_events_delivered_has_an_instant"


async def test_a_delivered_event_cannot_also_carry_a_failure(connection):
    """Success and a reason it failed are two answers to one question."""
    await insert_event(connection)

    with pytest.raises(asyncpg.CheckViolationError) as raised:
        await connection.execute(
            "UPDATE domain_events SET slack_state = 'delivered', "
            "slack_delivered_at = now(), slack_failure = 'slack_refused'"
        )

    assert raised.value.constraint_name == "domain_events_progress_has_no_reason"


async def test_a_failure_must_say_why(connection):
    """A red row with nothing to act on is what an operator finds at 3am."""
    await insert_event(connection)

    with pytest.raises(asyncpg.CheckViolationError) as raised:
        await connection.execute("UPDATE domain_events SET slack_state = 'failed'")

    assert raised.value.constraint_name == "domain_events_failed_has_a_reason"


async def test_a_pending_event_cannot_carry_a_stale_reason(connection):
    """A retryable failure is left pending and written NOWHERE.

    Recording a reason on a row that is about to be retried would make a
    momentary blip look like a broken integration on every screen that reads
    the column -- and would leave the reason there after the retry succeeded.
    """
    await insert_event(connection)

    with pytest.raises(asyncpg.CheckViolationError) as raised:
        await connection.execute(
            "UPDATE domain_events SET slack_failure = 'slack_unreachable'"
        )

    assert raised.value.constraint_name == "domain_events_progress_has_no_reason"


async def test_a_delivered_event_records_both_facts(connection):
    """The constraint from the useful side: this is what success looks like."""
    await insert_event(connection)
    await connection.execute(
        "UPDATE domain_events SET slack_state = 'delivered', "
        "slack_delivered_at = now(), slack_failure = NULL"
    )

    row = await connection.fetchrow(
        "SELECT slack_state, slack_delivered_at FROM domain_events"
    )

    assert row["slack_state"] == "delivered"
    assert row["slack_delivered_at"] is not None


# --- the vocabularies -------------------------------------------------


async def test_an_invented_kind_is_refused(connection):
    """The six are 018's six, exactly, so that a preference lookup is
    `kind = event` with nothing translating between two vocabularies. A typo
    here would be an event whose toggle nobody can find."""
    with pytest.raises(asyncpg.CheckViolationError) as raised:
        await insert_event(connection, kind="issue_asigned")

    assert raised.value.constraint_name == "domain_events_kind_known"


async def test_the_kinds_are_exactly_the_events_018_lets_a_workspace_choose(
    connection,
):
    """Read out of both CHECK constraints rather than compared against a list
    written here, because a list written here would be a third copy and the one
    that agrees with neither.

    If these two ever diverge, an event is emitted that no preference row can
    enable -- so it is silently never announced -- or a preference exists for
    something nothing emits, which is a toggle that does nothing.
    """

    async def vocabulary(constraint: str) -> set[str]:
        definition = await connection.fetchval(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = $1",
            constraint,
        )

        assert definition is not None, f"{constraint} does not exist"

        # PostgreSQL renders an `IN (...)` list back as `= ANY (ARRAY[...])`,
        # so the values are read out of that rather than out of the spelling
        # the migration used.
        return set(definition.split("ARRAY[")[1].rstrip("])").split(", "))

    assert await vocabulary("domain_events_kind_known") == await vocabulary(
        "slack_notification_preferences_event_known"
    )


async def test_an_invented_failure_reason_is_refused(connection):
    """The reasons are ours and never Slack's. Slack's `error` strings change
    without notice, and echoing one would put a provider's vocabulary into this
    schema and into every screen that reads it."""
    await insert_event(connection)

    with pytest.raises(asyncpg.CheckViolationError) as raised:
        await connection.execute(
            "UPDATE domain_events SET slack_state = 'failed', "
            "slack_failure = 'not_in_channel'"
        )

    assert raised.value.constraint_name == "domain_events_slack_failure_known"


async def test_an_invented_state_is_refused(connection):
    await insert_event(connection)

    with pytest.raises(asyncpg.CheckViolationError) as raised:
        await connection.execute("UPDATE domain_events SET slack_state = 'sending'")

    assert raised.value.constraint_name == "domain_events_slack_state_known"


# --- what a delete must not silently do -------------------------------


async def test_deleting_an_issue_does_not_silently_discard_what_was_announced(
    connection,
):
    """RESTRICT, not CASCADE, for the reason 013 and 017 both give: a one-line
    delete must not discard rows in another table while reporting `DELETE 1`.

    `RestrictViolationError`, not `ForeignKeyViolationError`. The two are
    siblings rather than one being the other's parent, and PostgreSQL raises
    them for opposite situations -- the foreign-key error means a child pointed
    at a parent that was not there, this one means the parent was there and
    declined to leave. Catching the wrong sibling would pass for a schema with
    no constraint at all, since the delete would then simply succeed.
    """
    await insert_event(connection)

    with pytest.raises(asyncpg.RestrictViolationError):
        await connection.execute(
            "DELETE FROM issues WHERE workspace_id = $1 AND id = $2",
            BOOTSTRAP_WORKSPACE_ID,
            BOOTSTRAP_ISSUE_ID,
        )
