"""Bulk actions against a real database, where all-or-nothing is a rollback.

`tests/test_bulk_actions.py` proves that a batch with an id this workspace does
not hold is REFUSED. That is only half the promise. The other half -- and the
one no fake can demonstrate, because a fake has no transaction -- is that the
issues in the batch that WERE the caller's come out unchanged.

The distinction matters because the wrong implementation passes the first half.
`UPDATE ... WHERE workspace_id = $1 AND id = ANY($2)` with one foreign id
updates eleven of twelve rows and reports success; a service that noticed the
count afterwards and raised outside the transaction would report an error to
the client while leaving eleven issues moved. The tests below read the rows
back after the refusal and assert that every one of them still holds its
original value.

The same shape covers a triage queue: accepting, declining and marking a
duplicate are single-issue operations, but marking a duplicate writes two rows
in one transaction and the relation must not survive a decline that failed.

Marked `db`: deselected by default, skipped when Docker is unreachable.
"""

import itertools
from uuid import UUID

import asyncpg
import pytest

from app.domain.bulk import BulkIssuePatch
from app.domain.errors import ValidationError
from app.domain.tenancy import WorkspaceScope
from app.repositories.bulk import BulkRepository
from app.repositories.issue_labels import IssueLabelRepository
from app.repositories.relations import RelationRepository
from app.repositories.teams import TeamRepository
from app.repositories.triage import TriageRepository
from app.services.bulk import BulkService
from app.services.teams import TeamService
from app.services.triage import TriageService

from tests.conftest import (
    apply_all_migrations,
    reset_schema,
    seed_workflow_states,
)


pytestmark = pytest.mark.db

# The tenant 002 seeds, named there as literals precisely so a test can assert
# against a constant instead of querying for the value it is about to check.
WORKSPACE_ID = UUID("00000000-0000-7000-8000-000000000001")
TEAM_ID = UUID("00000000-0000-7000-8000-000000000002")

# The other tenant, and it has to be REAL. A nonexistent id would be refused by
# any spelling of these checks and would prove nothing about which one is in
# force: the interesting id is one that exists, belongs to somebody else, and
# is therefore invisible to a statement scoped to the caller's workspace.
OTHER_WORKSPACE_ID = UUID("00000000-0000-7000-8000-0000000000a1")
OTHER_TEAM_ID = UUID("00000000-0000-7000-8000-0000000000a2")

# A second team in OUR workspace, for the triage move.
SECOND_TEAM_ID = UUID("00000000-0000-7000-8000-0000000000a3")

SCOPE = WorkspaceScope(workspace_id=WORKSPACE_ID)

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
    $5, 0
)
"""


@pytest.fixture
async def wired(postgres_dsn):
    """Two tenants, three teams, and the services under test over a real pool."""
    connection = await asyncpg.connect(postgres_dsn)

    try:
        await reset_schema(connection)
        await apply_all_migrations(connection)
        await seed(connection)

        pool = await asyncpg.create_pool(dsn=postgres_dsn, min_size=1, max_size=3)

        try:
            team_service = TeamService(pool=pool, repository=TeamRepository())

            yield (
                BulkService(
                    pool=pool,
                    repository=BulkRepository(),
                    issue_labels=IssueLabelRepository(),
                ),
                TriageService(
                    pool=pool,
                    repository=TriageRepository(),
                    relations=RelationRepository(),
                    teams=team_service,
                ),
                connection,
            )
        finally:
            await pool.close()
    finally:
        await reset_schema(connection)
        await connection.close()


async def seed(connection) -> None:
    await connection.execute(
        "INSERT INTO workspaces (id, slug, name) VALUES ($1, 'other', 'Other')",
        OTHER_WORKSPACE_ID,
    )

    for team_id, workspace_id, name, key in (
        (OTHER_TEAM_ID, OTHER_WORKSPACE_ID, "Other", "OTH"),
        (SECOND_TEAM_ID, WORKSPACE_ID, "Design", "DES"),
    ):
        await connection.execute(
            "INSERT INTO teams (id, workspace_id, name, key) VALUES ($1, $2, $3, $4)",
            team_id,
            workspace_id,
            name,
            key,
        )

        # 005 seeds workflow states for the teams that exist when it runs, so a
        # team created afterwards has none -- and an issue on it cannot be
        # inserted at all, because workflow_state_id is NOT NULL.
        await seed_workflow_states(connection, workspace_id, team_id)


# Issue ids, allocated in ascending order across the whole module.
#
# Ascending because two of the tests below read a queue ordered by
# `(triage_entered_at, id)`, and `id` is the tie-break that makes that ordering
# total. Random ids would make the assertion depend on which uuid4 happened to
# sort first whenever two issues entered a queue in the same microsecond --
# which is a flake that would appear roughly never and then in CI.
#
# Module-level rather than per-fixture: the database is dropped and rebuilt for
# every test, so ids only have to be unique within one, and a counter that
# never resets is one fewer thing to reset.
_next_issue_id = itertools.count(0x1000)


async def make_issue(
    connection,
    number: int,
    *,
    workspace_id: UUID = WORKSPACE_ID,
    team_id: UUID = TEAM_ID,
) -> UUID:
    """One issue, with `number` scoped to its own team.

    The id is allocated here rather than derived from `number`, because
    `issues_team_number_key` is unique per TEAM: two teams -- and two tenants
    -- legitimately both have a number 1, and deriving the id from it would
    make those two rows the same row.

    The team's counter is moved past the number afterwards, which is the same
    thing migration 005's last statement does after its backfill and is
    required for the same reason. This insert writes a number without asking
    the allocator for one, so a team seeded this way still reads
    `issue_counter = 0` -- and the next allocation, which is what
    `triageChangeTeam` performs, would hand out 1 and collide with the row
    seeded here. That would be a failure of the FIXTURE reported as a failure
    of the feature.
    """
    issue_id = UUID(int=next(_next_issue_id))

    await connection.execute(
        INSERT_ISSUE_SQL,
        issue_id,
        workspace_id,
        team_id,
        number,
        f"Issue {number}",
    )
    await connection.execute(
        "UPDATE teams SET issue_counter = GREATEST(issue_counter, $2) WHERE id = $1",
        team_id,
        number,
    )

    return issue_id


async def priorities(connection, issue_ids) -> list[int]:
    rows = await connection.fetch(
        "SELECT id, priority FROM issues WHERE id = ANY($1::UUID[]) ORDER BY id",
        list(issue_ids),
    )

    return [row["priority"] for row in rows]


# --- the property this module exists for -------------------------------


async def test_one_foreign_workspace_id_leaves_every_other_issue_untouched(wired):
    """THE test. Not "all but that one" -- nothing at all.

    Three issues are ours and a fourth belongs to another tenant. The batch
    names all four and asks for a priority change. The refusal is easy to get
    right; what is easy to get WRONG is the rollback, because the naive
    implementation has already issued an UPDATE that moved our three by the
    time it notices the count. So this reads the three back and asserts they
    still hold the priority they were created with.

    The foreign id is a real issue in a real workspace. An invented id would
    make this pass against a schema with no tenancy at all.
    """
    bulk, _, connection = wired

    ours = [await make_issue(connection, number) for number in (1, 2, 3)]
    theirs = await make_issue(
        connection, 1, workspace_id=OTHER_WORKSPACE_ID, team_id=OTHER_TEAM_ID
    )

    assert await priorities(connection, ours) == [0, 0, 0]

    with pytest.raises(ValidationError) as raised:
        await bulk.update_many(
            scope=SCOPE,
            issue_ids=[*ours, theirs],
            patch=BulkIssuePatch(priority=1),
        )

    assert raised.value.issues[0].code == "NOT_FOUND"
    assert await priorities(connection, ours) == [0, 0, 0]


async def test_the_foreign_issue_is_untouched_too(wired):
    """The other direction, which is the one an attacker is actually after.

    A refusal that rolled our three back while having written the fourth would
    be a cross-tenant write reported as an error. The statement is scoped, so
    the row was never in range -- asserted rather than assumed, because "it
    cannot have been" is exactly the reasoning that stops being true when
    somebody widens a predicate.
    """
    bulk, _, connection = wired

    ours = await make_issue(connection, 1)
    theirs = await make_issue(
        connection, 1, workspace_id=OTHER_WORKSPACE_ID, team_id=OTHER_TEAM_ID
    )

    with pytest.raises(ValidationError):
        await bulk.update_many(
            scope=SCOPE,
            issue_ids=[ours, theirs],
            patch=BulkIssuePatch(priority=1),
        )

    assert await priorities(connection, [theirs]) == [0]


async def test_the_refusal_does_not_say_which_id_failed(wired):
    """A caller who could learn WHICH of a hundred ids was rejected could
    binary-search another tenant's issue ids at a hundred per request."""
    bulk, _, connection = wired

    ours = await make_issue(connection, 1)
    theirs = await make_issue(
        connection, 1, workspace_id=OTHER_WORKSPACE_ID, team_id=OTHER_TEAM_ID
    )

    with pytest.raises(ValidationError) as raised:
        await bulk.update_many(
            scope=SCOPE,
            issue_ids=[ours, theirs],
            patch=BulkIssuePatch(priority=1),
        )

    assert str(theirs) not in raised.value.issues[0].message
    assert str(ours) not in raised.value.issues[0].message


async def test_a_nonexistent_id_is_refused_exactly_as_a_foreign_one_is(wired):
    """The two must stay indistinguishable, or the batch becomes an existence
    oracle: same code, same field, same message."""
    bulk, _, connection = wired

    ours = await make_issue(connection, 1)
    theirs = await make_issue(
        connection, 1, workspace_id=OTHER_WORKSPACE_ID, team_id=OTHER_TEAM_ID
    )

    def refusal(other):
        return bulk.update_many(
            scope=SCOPE, issue_ids=[ours, other], patch=BulkIssuePatch(priority=1)
        )

    with pytest.raises(ValidationError) as foreign:
        await refusal(theirs)

    with pytest.raises(ValidationError) as absent:
        await refusal(UUID(int=0xDEAD))

    assert foreign.value.issues[0] == absent.value.issues[0]


# --- the ordinary path, so the refusals are not vacuous -----------------


async def test_a_batch_of_our_own_issues_applies_to_all_of_them(wired):
    """The feature, before the refusals. A file of nothing but rejections would
    pass against a service that refused everything."""
    bulk, _, connection = wired

    ours = [await make_issue(connection, number) for number in (1, 2, 3)]

    changed = await bulk.update_many(
        scope=SCOPE, issue_ids=ours, patch=BulkIssuePatch(priority=2)
    )

    assert len(changed) == 3
    assert await priorities(connection, ours) == [2, 2, 2]


async def test_a_repeated_id_is_applied_once_and_is_not_a_refusal(wired):
    """`[a, a, b]` names two issues. A comparison against the raw list would
    tell a client that double-clicked that its batch was unauthorised."""
    bulk, _, connection = wired

    first = await make_issue(connection, 1)
    second = await make_issue(connection, 2)

    changed = await bulk.update_many(
        scope=SCOPE,
        issue_ids=[first, first, second],
        patch=BulkIssuePatch(priority=3),
    )

    assert len(changed) == 2
    assert await priorities(connection, [first, second]) == [3, 3]


async def test_a_bulk_update_records_one_activity_row_per_field_that_moved(wired):
    """History is written inside the same transaction as the change, so a
    rolled-back batch leaves no record of a batch. Only fields that actually
    MOVED are recorded -- setting a priority every issue already had records
    nothing."""
    bulk, _, connection = wired

    ours = [await make_issue(connection, number) for number in (1, 2)]

    await bulk.update_many(
        scope=SCOPE, issue_ids=ours, patch=BulkIssuePatch(priority=4)
    )

    moved = await connection.fetchval(
        "SELECT count(*) FROM issue_activity "
        "WHERE kind = 'priority_changed' AND issue_id = ANY($1::UUID[])",
        ours,
    )

    assert moved == 2

    await bulk.update_many(
        scope=SCOPE, issue_ids=ours, patch=BulkIssuePatch(priority=4)
    )

    still = await connection.fetchval(
        "SELECT count(*) FROM issue_activity "
        "WHERE kind = 'priority_changed' AND issue_id = ANY($1::UUID[])",
        ours,
    )

    assert still == 2


# --- bulk labels --------------------------------------------------------


async def test_a_bulk_label_add_refuses_the_batch_for_one_foreign_label(wired):
    """Label ids are attacker-controlled too, and are checked the same way.

    The count is separate from the attach because the attach uses ON CONFLICT
    DO NOTHING and cannot tell "already attached" from "no such label" in its
    own result -- so a batch whose labels were half real would otherwise apply
    the real ones and report success.
    """
    bulk, _, connection = wired

    ours = [await make_issue(connection, number) for number in (1, 2)]

    mine = await connection.fetchval(
        "INSERT INTO labels (workspace_id, name, color) "
        "VALUES ($1, 'bug', '#ff0000') RETURNING id",
        WORKSPACE_ID,
    )
    theirs = await connection.fetchval(
        "INSERT INTO labels (workspace_id, name, color) "
        "VALUES ($1, 'secret', '#00ff00') RETURNING id",
        OTHER_WORKSPACE_ID,
    )

    with pytest.raises(ValidationError) as raised:
        await bulk.update_many(
            scope=SCOPE, issue_ids=ours, add_label_ids=[mine, theirs]
        )

    assert raised.value.issues[0].code == "NOT_FOUND"

    attached = await connection.fetchval(
        "SELECT count(*) FROM issue_labels WHERE issue_id = ANY($1::UUID[])", ours
    )

    assert attached == 0


async def test_a_bulk_label_add_carries_the_labels_exclusivity_key(wired):
    """The join row's `exclusivity_key` is read from the label in the same
    statement, so the bulk path cannot store an association whose exclusivity
    claim its label does not hold."""
    bulk, _, connection = wired

    ours = [await make_issue(connection, number) for number in (1, 2)]

    group_id = await connection.fetchval(
        "INSERT INTO label_groups (workspace_id, name, exclusive) "
        "VALUES ($1, 'Status', TRUE) RETURNING id",
        WORKSPACE_ID,
    )
    label_id = await connection.fetchval(
        "INSERT INTO labels (workspace_id, name, color, group_id, group_exclusive) "
        "VALUES ($1, 'bug', '#ff0000', $2, TRUE) RETURNING id",
        WORKSPACE_ID,
        group_id,
    )

    await bulk.update_many(scope=SCOPE, issue_ids=ours, add_label_ids=[label_id])

    keys = await connection.fetch(
        "SELECT exclusivity_key FROM issue_labels WHERE issue_id = ANY($1::UUID[])",
        ours,
    )

    assert [row["exclusivity_key"] for row in keys] == [group_id, group_id]


async def test_a_bulk_add_from_an_exclusive_group_refuses_the_whole_batch(wired):
    """ "Add Urgent to these twelve" when three already carry Low from the same
    exclusive group is a request that cannot be satisfied. Satisfying nine of
    it would be worse than refusing it, so the unique index takes the batch
    down and the rollback puts the nine back."""
    bulk, _, connection = wired

    ours = [await make_issue(connection, number) for number in (1, 2)]

    group_id = await connection.fetchval(
        "INSERT INTO label_groups (workspace_id, name, exclusive) "
        "VALUES ($1, 'Status', TRUE) RETURNING id",
        WORKSPACE_ID,
    )
    low, urgent = [
        await connection.fetchval(
            "INSERT INTO labels "
            "(workspace_id, name, color, group_id, group_exclusive) "
            "VALUES ($1, $2, '#ff0000', $3, TRUE) RETURNING id",
            WORKSPACE_ID,
            name,
            group_id,
        )
        for name in ("low", "urgent")
    ]

    await bulk.update_many(scope=SCOPE, issue_ids=[ours[0]], add_label_ids=[low])

    with pytest.raises(ValidationError) as raised:
        await bulk.update_many(scope=SCOPE, issue_ids=ours, add_label_ids=[urgent])

    assert raised.value.issues[0].code == "EXCLUSIVE_GROUP"

    # The issue that COULD have taken it did not, which is the rollback.
    urgent_rows = await connection.fetchval(
        "SELECT count(*) FROM issue_labels WHERE label_id = $1", urgent
    )

    assert urgent_rows == 0


# --- bulk archive -------------------------------------------------------


async def test_a_bulk_archive_refuses_an_issue_still_in_triage(wired):
    """Archiving unaccepted work would empty a team's incoming queue as a side
    effect of somebody tidying a list. Migration 021 makes that a refusal, and
    the refusal takes the batch with it -- so the issues that were not in
    triage stay on the board."""
    bulk, triage, connection = wired

    ours = [await make_issue(connection, number) for number in (1, 2)]

    await triage.enter(scope=SCOPE, issue_id=ours[1])

    with pytest.raises(ValidationError) as raised:
        await bulk.archive_many(scope=SCOPE, issue_ids=ours)

    assert raised.value.issues[0].code == "IN_TRIAGE"

    archived = await connection.fetchval(
        "SELECT count(*) FROM issues "
        "WHERE id = ANY($1::UUID[]) AND archived_at IS NOT NULL",
        ours,
    )

    assert archived == 0


async def test_a_bulk_archive_refuses_an_already_archived_id(wired):
    """Stricter than it strictly needs to be, and the right strictness: a
    client selecting a list it had already archived half of is working from a
    stale view, and telling it so is more useful than a success that moved
    nothing."""
    bulk, _, connection = wired

    ours = [await make_issue(connection, number) for number in (1, 2)]

    await bulk.archive_many(scope=SCOPE, issue_ids=[ours[0]])

    with pytest.raises(ValidationError):
        await bulk.archive_many(scope=SCOPE, issue_ids=ours)

    archived = await connection.fetchval(
        "SELECT count(*) FROM issues "
        "WHERE id = ANY($1::UUID[]) AND archived_at IS NOT NULL",
        ours,
    )

    assert archived == 1


# --- triage, end to end -------------------------------------------------


async def test_an_issue_enters_a_queue_and_is_accepted_out_of_it(wired):
    """The ordinary path. Accepting into a terminal state also stamps
    `completed_at`, through the same expression the single-issue update uses --
    so an issue accepted straight into Done is complete, and one accepted into
    Todo is not."""
    _, triage, connection = wired

    issue_id = await make_issue(connection, 1)

    await triage.enter(scope=SCOPE, issue_id=issue_id)

    page = await triage.queue(scope=SCOPE, team_id=TEAM_ID, first=25, after=None)

    assert [node.issue.id for node in page.nodes] == [issue_id]
    assert await triage.waiting_count(scope=SCOPE, team_id=TEAM_ID) == 1

    done = await connection.fetchval(
        "SELECT id FROM workflow_states "
        "WHERE workspace_id = $1 AND team_id = $2 AND type = 'completed'",
        WORKSPACE_ID,
        TEAM_ID,
    )

    accepted = await triage.accept(
        scope=SCOPE, issue_id=issue_id, workflow_state_id=done
    )

    assert accepted.workflow_state_id == done
    assert accepted.completed_at is not None
    assert await triage.waiting_count(scope=SCOPE, team_id=TEAM_ID) == 0


async def test_a_declined_issue_lands_on_its_own_teams_canceled_state(wired):
    """Resolved by CATEGORY from the issue's own team, in the same statement --
    so no team id arrives from a client and no state name is compared."""
    _, triage, connection = wired

    issue_id = await make_issue(connection, 1)

    await triage.enter(scope=SCOPE, issue_id=issue_id)

    declined = await triage.decline(scope=SCOPE, issue_id=issue_id)

    category = await connection.fetchval(
        "SELECT type FROM workflow_states WHERE id = $1", declined.workflow_state_id
    )

    assert category == "canceled"
    assert declined.completed_at is not None


async def test_marking_a_duplicate_writes_one_relation_and_declines(wired):
    """Reuses `issue_relations` and invents nothing. One canonical row, of type
    `duplicate`, which migration 010 orders so the pair cannot be stored twice
    in the other direction."""
    _, triage, connection = wired

    issue_id = await make_issue(connection, 1)
    original = await make_issue(connection, 2)

    await triage.enter(scope=SCOPE, issue_id=issue_id)

    await triage.mark_duplicate(
        scope=SCOPE, issue_id=issue_id, duplicate_of_id=original
    )

    relations = await connection.fetch(
        "SELECT source_issue_id, target_issue_id, type FROM issue_relations"
    )

    assert len(relations) == 1
    assert relations[0]["type"] == "duplicate"
    assert {relations[0]["source_issue_id"], relations[0]["target_issue_id"]} == {
        issue_id,
        original,
    }

    assert await triage.waiting_count(scope=SCOPE, team_id=TEAM_ID) == 0


async def test_a_duplicate_of_another_workspaces_issue_leaves_the_queue_alone(
    wired,
):
    """The relation is written FIRST, so a pair that cannot be related leaves
    the issue in the queue rather than declining it for a reason the server
    could not record."""
    _, triage, connection = wired

    issue_id = await make_issue(connection, 1)
    theirs = await make_issue(
        connection, 1, workspace_id=OTHER_WORKSPACE_ID, team_id=OTHER_TEAM_ID
    )

    await triage.enter(scope=SCOPE, issue_id=issue_id)

    with pytest.raises(ValidationError) as raised:
        await triage.mark_duplicate(
            scope=SCOPE, issue_id=issue_id, duplicate_of_id=theirs
        )

    assert raised.value.issues[0].code == "NOT_FOUND"
    assert await triage.waiting_count(scope=SCOPE, team_id=TEAM_ID) == 1
    assert await connection.fetchval("SELECT count(*) FROM issue_relations") == 0


async def test_changing_a_queued_issues_team_renumbers_it_and_keeps_it_queued(
    wired,
):
    """The one place in this product where an identifier changes.

    Defensible only because the issue is still in triage: nobody has accepted
    it, nothing links to it, and the team it was filed against was a guess. The
    number comes off the TARGET team's counter, so it cannot collide with
    whatever holds that number there.
    """
    _, triage, connection = wired

    existing = await make_issue(connection, 1, team_id=SECOND_TEAM_ID)
    issue_id = await make_issue(connection, 1)

    await triage.enter(scope=SCOPE, issue_id=issue_id)

    moved = await triage.change_team(
        scope=SCOPE, issue_id=issue_id, team_id=SECOND_TEAM_ID
    )

    # DES-2, not DES-1: the target team already holds number 1, and the new
    # number comes off that team's counter rather than travelling with the
    # issue. An implementation that carried the old number over would collide
    # with `existing` on issues_team_number_key.
    assert moved.team_id == SECOND_TEAM_ID
    assert moved.identifier == "DES-2"
    assert moved.number == 2

    # It left the old queue and joined the new one, which is what a per-team
    # read of one column already means.
    assert await triage.waiting_count(scope=SCOPE, team_id=TEAM_ID) == 0
    assert await triage.waiting_count(scope=SCOPE, team_id=SECOND_TEAM_ID) == 1

    # And it did not disturb the issue that was already there.
    assert (
        await connection.fetchval("SELECT number FROM issues WHERE id = $1", existing)
        == 1
    )


async def test_an_issue_that_has_left_triage_cannot_be_renumbered(wired):
    """The predicate that makes the renumbering defensible, asserted rather
    than described. An issue somebody is working on has an identifier other
    people are using."""
    _, triage, connection = wired

    issue_id = await make_issue(connection, 1)

    with pytest.raises(ValidationError) as raised:
        await triage.change_team(scope=SCOPE, issue_id=issue_id, team_id=SECOND_TEAM_ID)

    assert raised.value.issues[0].code == "NOT_FOUND"


async def test_a_failed_team_change_returns_the_allocated_number(wired):
    """005's counter is gapless only if every transaction that allocates a
    number either uses it or rolls back. A refusal that committed the increment
    would leave a permanent hole in the target team's numbering."""
    _, triage, connection = wired

    issue_id = await make_issue(connection, 1)

    before = await connection.fetchval(
        "SELECT issue_counter FROM teams WHERE id = $1", SECOND_TEAM_ID
    )

    with pytest.raises(ValidationError):
        # Not in triage, so the UPDATE matches nothing after the allocation.
        await triage.change_team(scope=SCOPE, issue_id=issue_id, team_id=SECOND_TEAM_ID)

    after = await connection.fetchval(
        "SELECT issue_counter FROM teams WHERE id = $1", SECOND_TEAM_ID
    )

    assert after == before


async def test_a_queue_read_cannot_reach_another_workspaces_team(wired):
    """An empty page rather than an error, exactly as a team with an empty
    queue gives. Any distinguishable answer would tell a caller holding a
    guessed id that the team is real and simply not theirs."""
    _, triage, connection = wired

    theirs = await make_issue(
        connection, 1, workspace_id=OTHER_WORKSPACE_ID, team_id=OTHER_TEAM_ID
    )

    await connection.execute(
        "UPDATE issues SET triage_entered_at = now() WHERE id = $1", theirs
    )

    page = await triage.queue(scope=SCOPE, team_id=OTHER_TEAM_ID, first=25, after=None)

    assert page.nodes == []
    assert await triage.waiting_count(scope=SCOPE, team_id=OTHER_TEAM_ID) == 0


async def test_the_queue_walks_a_page_at_a_time_in_arrival_order(wired):
    """Oldest first, and the keyset is total over `(entered_at, id)` -- which
    is what keeps a page walk from repeating or skipping a row when a bulk
    import puts several issues in a queue in the same microsecond."""
    _, triage, connection = wired

    ids = [await make_issue(connection, number) for number in (1, 2, 3)]

    for issue_id in ids:
        await triage.enter(scope=SCOPE, issue_id=issue_id)

    first = await triage.queue(scope=SCOPE, team_id=TEAM_ID, first=2, after=None)

    assert [node.issue.id for node in first.nodes] == ids[:2]
    assert first.has_next_page

    second = await triage.queue(
        scope=SCOPE, team_id=TEAM_ID, first=2, after=first.end_cursor
    )

    assert [node.issue.id for node in second.nodes] == ids[2:]
    assert not second.has_next_page
