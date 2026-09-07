"""Migration 021 applied by the real runner, then asked what it built.

021 makes two claims a service could also have made, and the point of this
file is that it makes them where a service cannot be forgotten.

The first is exclusivity. "An issue may wear at most one label from an
exclusive group" is the kind of rule that normally lives in a count-then-insert
in application code, which is racy by construction: two attaches can both read
zero and both succeed. Here it is a partial unique index over a generated
column, so the second attach is refused by the server, and the assertions below
are mostly about rows PostgreSQL will not store:

  * two labels from one exclusive group on one issue;
  * an association whose carried `exclusivity_key` is not its label's;
  * a label claiming a group's id while claiming the group is not exclusive,
    which is the one spelling that would switch the rule off silently;
  * a label in a group from another workspace;
  * making a group exclusive while an issue already breaks the rule.

and two that are about the shape being useful rather than merely safe: two
labels from a NON-exclusive group coexist on an issue, and the same ungrouped
label attached twice is still reported as a duplicate rather than as a group
conflict -- the distinction the index's predicate exists to preserve.

The second claim is triage. An issue in triage is one with an instant in
`triage_entered_at`, and the assertions for it are that the queue is per team,
that the column is the ordering key, and that an archived issue cannot be
sitting in one.

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

# The other tenant: a real workspace with a real team, a real issue and a real
# label group, whose rows every cross-tenant assertion below tries and fails to
# reach. It has to be real -- a nonexistent id would be refused by any spelling
# of these constraints and would prove nothing about which one is in force.
OTHER_WORKSPACE_ID = UUID("00000000-0000-7000-8000-0000000000a1")
OTHER_TEAM_ID = UUID("00000000-0000-7000-8000-0000000000a2")

# A second team in the BOOTSTRAP workspace, which is what makes "the queue is
# per team" a claim this file can test. A triage assertion against one team
# proves nothing about scoping.
SECOND_TEAM_ID = UUID("00000000-0000-7000-8000-0000000000a3")

ISSUE_ID = UUID("00000000-0000-7000-8000-0000000000b1")
OTHER_ISSUE_ID = UUID("00000000-0000-7000-8000-0000000000b2")
SECOND_TEAM_ISSUE_ID = UUID("00000000-0000-7000-8000-0000000000b3")

EXCLUSIVE_GROUP_ID = UUID("00000000-0000-7000-8000-0000000000c1")
OPEN_GROUP_ID = UUID("00000000-0000-7000-8000-0000000000c2")
OTHER_GROUP_ID = UUID("00000000-0000-7000-8000-0000000000c3")

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

INSERT_GROUP_SQL = """
INSERT INTO label_groups (id, workspace_id, name, exclusive)
VALUES ($1, $2, $3, $4)
"""

# `group_exclusive` is written by hand here because this file is about the
# schema and not about the repository that normally supplies it. Every
# assertion below that supplies the WRONG value is testing exactly that
# labels_group_fk notices.
INSERT_LABEL_SQL = """
INSERT INTO labels (workspace_id, name, color, group_id, group_exclusive)
VALUES ($1, $2, '#6b7280', $3, $4)
RETURNING id
"""

# The shape every attach in the application uses: the carried key is READ from
# the label in the same statement, so there is no window in which it could be
# read and then invalidated, and no parameter through which a caller could
# supply a key of its own.
ATTACH_SQL = """
INSERT INTO issue_labels (workspace_id, issue_id, label_id, exclusivity_key)
SELECT $1, $2, labels.id, labels.exclusivity_key
FROM labels
WHERE labels.workspace_id = $1 AND labels.id = $3
"""

ENTER_TRIAGE_SQL = """
UPDATE issues SET triage_entered_at = now() WHERE workspace_id = $1 AND id = $2
"""


@pytest.fixture
async def connection(postgres_dsn):
    """A migrated database with two tenants and three teams."""
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
    """Two workspaces with a group each, and three teams with an issue each.

    Symmetric across the tenant boundary on purpose. Every cross-tenant
    assertion below is "workspace A's row pointing at workspace B's row", and a
    lopsided fixture -- where one side lacks the thing the other reaches for --
    would pass those assertions for the wrong reason.
    """
    await connection.execute(
        "INSERT INTO workspaces (id, slug, name) VALUES ($1, 'other', 'Other')",
        OTHER_WORKSPACE_ID,
    )

    for team_id, workspace_id, name, key in (
        (OTHER_TEAM_ID, OTHER_WORKSPACE_ID, "Other", "OTH"),
        (SECOND_TEAM_ID, BOOTSTRAP_WORKSPACE_ID, "Design", "DES"),
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

    for issue_id, workspace_id, team_id, title in (
        (ISSUE_ID, BOOTSTRAP_WORKSPACE_ID, BOOTSTRAP_TEAM_ID, "Ours"),
        (OTHER_ISSUE_ID, OTHER_WORKSPACE_ID, OTHER_TEAM_ID, "Theirs"),
        (SECOND_TEAM_ISSUE_ID, BOOTSTRAP_WORKSPACE_ID, SECOND_TEAM_ID, "Design work"),
    ):
        await connection.execute(
            INSERT_ISSUE_SQL, issue_id, workspace_id, team_id, 1, title
        )

    for group_id, workspace_id, name, exclusive in (
        (EXCLUSIVE_GROUP_ID, BOOTSTRAP_WORKSPACE_ID, "Status", True),
        (OPEN_GROUP_ID, BOOTSTRAP_WORKSPACE_ID, "Area", False),
        (OTHER_GROUP_ID, OTHER_WORKSPACE_ID, "Status", True),
    ):
        await connection.execute(
            INSERT_GROUP_SQL, group_id, workspace_id, name, exclusive
        )


async def make_label(
    connection,
    name: str,
    *,
    workspace_id: UUID = BOOTSTRAP_WORKSPACE_ID,
    group_id: UUID | None = None,
    exclusive: bool | None = None,
) -> UUID:
    label_id: UUID = await connection.fetchval(
        INSERT_LABEL_SQL, workspace_id, name, group_id, exclusive
    )

    return label_id


# --- the ordinary paths work ------------------------------------------


async def test_two_labels_from_a_non_exclusive_group_share_an_issue(connection):
    """The feature, before the refusals.

    A group with `exclusive = false` is a folder and nothing more: an issue may
    wear every label in it. Asserted first because a schema that refused this
    would pass every exclusivity test below while making label groups useless.
    """
    for name in ("Frontend", "Backend"):
        label_id = await make_label(
            connection, name, group_id=OPEN_GROUP_ID, exclusive=False
        )
        await connection.execute(ATTACH_SQL, BOOTSTRAP_WORKSPACE_ID, ISSUE_ID, label_id)

    assert (
        await connection.fetchval(
            "SELECT count(*) FROM issue_labels WHERE issue_id = $1", ISSUE_ID
        )
        == 2
    )


async def test_one_label_from_an_exclusive_group_is_stored(connection):
    """The other half of the same point: exclusivity refuses the SECOND label,
    not the first."""
    label_id = await make_label(
        connection, "Bug", group_id=EXCLUSIVE_GROUP_ID, exclusive=True
    )
    await connection.execute(ATTACH_SQL, BOOTSTRAP_WORKSPACE_ID, ISSUE_ID, label_id)

    assert (
        await connection.fetchval(
            "SELECT exclusivity_key FROM issue_labels WHERE issue_id = $1", ISSUE_ID
        )
        == EXCLUSIVE_GROUP_ID
    )


async def test_an_ungrouped_labels_key_is_its_own_id(connection):
    """The ELSE branch of the generated column, which is what keeps the partial
    index from ever seeing an ungrouped label.

    A label with no group competes only for itself, so its key is its own id --
    unique across the table, so it can never collide with another label's.
    """
    label_id = await make_label(connection, "Loose")

    assert (
        await connection.fetchval(
            "SELECT exclusivity_key FROM labels WHERE id = $1", label_id
        )
        == label_id
    )


# --- the exclusivity refusals -----------------------------------------


async def test_two_labels_from_an_exclusive_group_cannot_share_an_issue(connection):
    """The rule this migration exists for.

    Both labels resolve to the GROUP's id as their exclusivity key, so the two
    associations collide on issue_labels_exclusive_group_key. Refused by the
    server rather than by a service that counted first -- which would be racy,
    since two concurrent attaches can both read a count of zero.
    """
    first = await make_label(
        connection, "Bug", group_id=EXCLUSIVE_GROUP_ID, exclusive=True
    )
    second = await make_label(
        connection, "Feature", group_id=EXCLUSIVE_GROUP_ID, exclusive=True
    )

    await connection.execute(ATTACH_SQL, BOOTSTRAP_WORKSPACE_ID, ISSUE_ID, first)

    with pytest.raises(asyncpg.UniqueViolationError) as raised:
        await connection.execute(ATTACH_SQL, BOOTSTRAP_WORKSPACE_ID, ISSUE_ID, second)

    assert raised.value.constraint_name == "issue_labels_exclusive_group_key"


async def test_the_same_exclusive_label_on_two_issues_is_fine(connection):
    """Exclusivity is per ISSUE, not per label.

    "One status per issue" must not become "one issue per status", which is
    what a key missing `issue_id` would have said.
    """
    label_id = await make_label(
        connection, "Bug", group_id=EXCLUSIVE_GROUP_ID, exclusive=True
    )

    for issue_id in (ISSUE_ID, SECOND_TEAM_ISSUE_ID):
        await connection.execute(ATTACH_SQL, BOOTSTRAP_WORKSPACE_ID, issue_id, label_id)

    assert (
        await connection.fetchval(
            "SELECT count(*) FROM issue_labels WHERE label_id = $1", label_id
        )
        == 2
    )


async def test_attaching_the_same_ungrouped_label_twice_is_a_duplicate(connection):
    """The distinction the index's PREDICATE exists to preserve.

    Without `WHERE exclusivity_key <> label_id` this row would violate both
    issue_labels_pkey and the exclusivity index, PostgreSQL would report
    whichever it checked first, and LabelService would answer "already applied"
    or "excluded by group" more or less at random. The predicate removes every
    ungrouped association from the index, so a duplicate attach can only ever
    be the primary key.
    """
    label_id = await make_label(connection, "Loose")

    await connection.execute(ATTACH_SQL, BOOTSTRAP_WORKSPACE_ID, ISSUE_ID, label_id)

    with pytest.raises(asyncpg.UniqueViolationError) as raised:
        await connection.execute(ATTACH_SQL, BOOTSTRAP_WORKSPACE_ID, ISSUE_ID, label_id)

    assert raised.value.constraint_name == "issue_labels_pkey"


async def test_an_association_cannot_carry_a_key_its_label_does_not_have(connection):
    """The carried value is checked, not merely written.

    This is the write a careless bulk importer makes: it supplies its own
    `exclusivity_key` instead of reading the label's, and every ungrouped value
    it invents would put the association outside the partial index and defeat
    the rule for that row. issue_labels_exclusivity_fk is what makes the column
    a copy rather than a claim.
    """
    label_id = await make_label(
        connection, "Bug", group_id=EXCLUSIVE_GROUP_ID, exclusive=True
    )

    with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
        await connection.execute(
            "INSERT INTO issue_labels "
            "(workspace_id, issue_id, label_id, exclusivity_key) "
            "VALUES ($1, $2, $3, $3)",
            BOOTSTRAP_WORKSPACE_ID,
            ISSUE_ID,
            label_id,
        )

    assert raised.value.constraint_name == "issue_labels_exclusivity_fk"


async def test_a_label_cannot_claim_a_group_while_denying_its_exclusivity(connection):
    """The one spelling that would switch the rule off silently.

    `group_exclusive` is the label's copy of the group's flag, and a label
    claiming `false` against an exclusive group would compute its key as its
    own id -- in the group, with exclusivity disabled, and every other
    constraint reporting success. labels_group_fk carries the flag in its
    referenced key so that pairing has no row to match.
    """
    with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
        await make_label(
            connection, "Sneaky", group_id=EXCLUSIVE_GROUP_ID, exclusive=False
        )

    assert raised.value.constraint_name == "labels_group_fk"


async def test_a_label_cannot_claim_a_group_while_leaving_the_flag_null(connection):
    """The hole MATCH SIMPLE leaves, closed by a CHECK.

    A composite foreign key skips its check entirely for a row with any NULL
    among its referencing columns, so `group_id` set with `group_exclusive`
    NULL would never reach labels_group_fk at all -- and would compute the same
    disabled key the previous test is about. This is 009's
    issues_milestone_requires_project applied to the pair here.
    """
    with pytest.raises(asyncpg.CheckViolationError) as raised:
        await make_label(connection, "Sneaky", group_id=EXCLUSIVE_GROUP_ID)

    assert raised.value.constraint_name == "labels_group_exclusive_paired"


async def test_a_label_cannot_join_another_workspaces_group(connection):
    """One workspace_id for the row, so this pairing has nowhere to be spelled.

    The label claims the bootstrap workspace and points at the other
    workspace's group. labels_group_fk reads the same workspace_id for both
    halves, so the pair simply does not exist -- and a label group cannot be
    used to learn that another tenant's group is real.
    """
    with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
        await make_label(connection, "Bug", group_id=OTHER_GROUP_ID, exclusive=True)

    assert raised.value.constraint_name == "labels_group_fk"


async def test_the_generated_key_cannot_be_written_by_hand(connection):
    """`exclusivity_key` is GENERATED ALWAYS, so there is no INSERT that sets
    it -- which is what makes it a derivation rather than a third copy for a
    writer to get wrong."""
    with pytest.raises(asyncpg.GeneratedAlwaysError):
        await connection.execute(
            "INSERT INTO labels (workspace_id, name, color, exclusivity_key) "
            "VALUES ($1, 'Forged', '#6b7280', $1)",
            BOOTSTRAP_WORKSPACE_ID,
        )


# --- changing a group's exclusivity -----------------------------------


async def test_making_a_group_exclusive_propagates_to_existing_associations(
    connection,
):
    """The first link of the cascade, on its own.

    Flipping `label_groups.exclusive` rewrites `labels.group_exclusive`, which
    recomputes the generated key, which rewrites `issue_labels.exclusivity_key`.
    Without that second hop the rule would apply only to labels attached after
    the flip.
    """
    label_id = await make_label(
        connection, "Frontend", group_id=OPEN_GROUP_ID, exclusive=False
    )
    await connection.execute(ATTACH_SQL, BOOTSTRAP_WORKSPACE_ID, ISSUE_ID, label_id)

    await connection.execute(
        "UPDATE label_groups SET exclusive = TRUE WHERE id = $1", OPEN_GROUP_ID
    )

    assert (
        await connection.fetchval(
            "SELECT exclusivity_key FROM issue_labels WHERE label_id = $1", label_id
        )
        == OPEN_GROUP_ID
    )


async def test_a_group_cannot_be_made_exclusive_while_an_issue_breaks_the_rule(
    connection,
):
    """The refusal that makes the cascade worth having.

    Two labels from a non-exclusive group sit on one issue. Making the group
    exclusive would give both associations the same key, which the partial
    unique index refuses -- so the flip fails atomically instead of leaving a
    workspace with a rule its own data violates. Under ON UPDATE RESTRICT this
    statement would have been refused for every group with any label in it,
    exclusivity would be a create-time decision only, and no such check would
    exist to make.
    """
    for name in ("Frontend", "Backend"):
        label_id = await make_label(
            connection, name, group_id=OPEN_GROUP_ID, exclusive=False
        )
        await connection.execute(ATTACH_SQL, BOOTSTRAP_WORKSPACE_ID, ISSUE_ID, label_id)

    with pytest.raises(asyncpg.UniqueViolationError) as raised:
        await connection.execute(
            "UPDATE label_groups SET exclusive = TRUE WHERE id = $1", OPEN_GROUP_ID
        )

    assert raised.value.constraint_name == "issue_labels_exclusive_group_key"


async def test_deleting_a_group_with_labels_in_it_is_refused(connection):
    """RESTRICT, not CASCADE, for the reason 009 gives about project_teams: a
    one-line delete must not discard rows in another table while reporting
    `DELETE 1`. LabelService ungroups the labels itself, first, in the same
    transaction.

    `RestrictViolationError`, not `ForeignKeyViolationError`. The two are
    siblings rather than one being the other's parent, and PostgreSQL raises
    them for opposite situations: the foreign-key error means a child pointed
    at a parent that was not there, this one means the parent was there and
    declined to leave.
    """
    await make_label(connection, "Bug", group_id=EXCLUSIVE_GROUP_ID, exclusive=True)

    with pytest.raises(asyncpg.RestrictViolationError):
        await connection.execute(
            "DELETE FROM label_groups WHERE id = $1", EXCLUSIVE_GROUP_ID
        )


async def test_group_names_are_unique_per_workspace_case_insensitively(connection):
    """A picker showing both "Status" and "status" offers a choice with no
    meaning, and a plain UNIQUE over TEXT is case-sensitive."""
    with pytest.raises(asyncpg.UniqueViolationError) as raised:
        await connection.execute(
            "INSERT INTO label_groups (workspace_id, name, exclusive) "
            "VALUES ($1, 'STATUS', TRUE)",
            BOOTSTRAP_WORKSPACE_ID,
        )

    assert raised.value.constraint_name == "label_groups_workspace_name_key"


async def test_two_workspaces_may_each_have_a_status_group(connection):
    """The other side of the same index: uniqueness is per workspace and
    emphatically not global, or one tenant's choice of group name would deny it
    to every other -- and a failed create would report the existence of a group
    in a workspace the caller cannot see."""
    assert (
        await connection.fetchval(
            "SELECT count(*) FROM label_groups WHERE lower(name) = 'status'"
        )
        == 2
    )


# --- triage -----------------------------------------------------------


async def test_an_issue_enters_and_leaves_triage(connection):
    """The column is the flag and the instant at once: non-NULL means waiting,
    since then; NULL means not in any queue."""
    await connection.execute(ENTER_TRIAGE_SQL, BOOTSTRAP_WORKSPACE_ID, ISSUE_ID)

    assert (
        await connection.fetchval(
            "SELECT triage_entered_at FROM issues WHERE id = $1", ISSUE_ID
        )
        is not None
    )

    await connection.execute(
        "UPDATE issues SET triage_entered_at = NULL WHERE id = $1", ISSUE_ID
    )

    assert (
        await connection.fetchval(
            "SELECT triage_entered_at FROM issues WHERE id = $1", ISSUE_ID
        )
        is None
    )


async def test_the_triage_queue_is_scoped_to_one_team(connection):
    """Triage is a TEAM-level queue, which is the product claim the whole
    feature rests on. Two issues in one workspace on two teams both enter
    triage; the read for one team returns one of them.

    Asserted with a second team in the SAME workspace rather than with a second
    workspace, because a workspace-scoped read would pass that weaker test
    while getting the product wrong.
    """
    for issue_id in (ISSUE_ID, SECOND_TEAM_ISSUE_ID):
        await connection.execute(ENTER_TRIAGE_SQL, BOOTSTRAP_WORKSPACE_ID, issue_id)

    queued = await connection.fetch(
        """
        SELECT id FROM issues
        WHERE workspace_id = $1 AND team_id = $2 AND triage_entered_at IS NOT NULL
        ORDER BY triage_entered_at, id
        """,
        BOOTSTRAP_WORKSPACE_ID,
        BOOTSTRAP_TEAM_ID,
    )

    assert [row["id"] for row in queued] == [ISSUE_ID]


async def test_an_archived_issue_cannot_be_in_triage(connection):
    """An archived issue is not waiting for anybody.

    Written as a constraint rather than left to every read to remember, so that
    whoever adds the first bulk-archive path has to decide what happens to the
    queue entry instead of leaving a row nothing will ever look at again.
    """
    await connection.execute(ENTER_TRIAGE_SQL, BOOTSTRAP_WORKSPACE_ID, ISSUE_ID)

    with pytest.raises(asyncpg.CheckViolationError) as raised:
        await connection.execute(
            "UPDATE issues SET archived_at = now() WHERE id = $1", ISSUE_ID
        )

    assert raised.value.constraint_name == "issues_triage_is_not_archived"


async def test_an_issue_that_never_entered_triage_archives_normally(connection):
    """The same constraint from the other side, so it is not merely a ban on
    archiving. Both columns are nullable and a CHECK treats NULL as satisfied,
    so the ordinary archive -- of an issue that was never in a queue -- has to
    keep working."""
    await connection.execute(
        "UPDATE issues SET archived_at = now() WHERE id = $1", ISSUE_ID
    )

    assert (
        await connection.fetchval(
            "SELECT archived_at FROM issues WHERE id = $1", ISSUE_ID
        )
        is not None
    )
