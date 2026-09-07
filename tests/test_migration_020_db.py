"""Migration 020 applied by the real runner, then asked what it built.

020 adds two things that both point at rows somebody else owns, and the
question this file exists to answer is what happens when one of them points at
a row in a workspace it has nothing to do with.

For SUBSCRIBERS the attack is small and cheap to attempt: a subscribe mutation
carries an issue id, and the only thing between a guessed id and a stream of
another tenant's notifications is the pair of composite keys below. There is no
column for a second workspace to go in, so the row is refused by the schema
rather than by the resolver remembering.

For TEMPLATES it is slower and worse. A template is stored, attacker-controlled
data NAMING OTHER ROWS -- an assignee, a project, a cycle, a set of labels --
written once and replayed by everyone who files from it. A mis-scoped id there
is not one bad request; it is one bad request replayed indefinitely, months
after whoever wrote it stopped watching. So every reference is checked at the
moment it is SAVED, by a key that reads the template's own single workspace_id.

The answer has to be the database, not the service. A resolver that scopes its
lookup correctly is right until someone writes a second one, and the second one
is where this class of bug lives. So the assertions below are mostly about rows
PostgreSQL will not store:

  * a subscriber who is not a member of the issue's workspace;
  * a subscription onto another workspace's issue;
  * a template naming another workspace's team, member, project or label;
  * a template naming another TEAM's cycle -- not merely another workspace's,
    which is what the three-column key buys over a two-column one;
  * a workspace-wide template carrying a cycle at all, which is the hole
    MATCH SIMPLE would otherwise leave open.

And several about the shape being useful rather than safe: the widened
notification vocabulary admits 'status_changed' and still refuses an invented
kind, the subscriber primary key makes a re-subscribe absorbable, and deleting
a label a template uses is refused rather than silently stripping it.

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

# The other tenant: a real workspace with a real team, member, issue, project,
# cycle and label, every one of which the assertions below try and fail to
# reach. It has to be real -- a nonexistent id would be refused by any spelling
# of these constraints and would prove nothing about which one is in force.
OTHER_WORKSPACE_ID = UUID("00000000-0000-7000-8000-0000000000a1")
OTHER_TEAM_ID = UUID("00000000-0000-7000-8000-0000000000a2")

# A second team inside the BOOTSTRAP workspace. Same tenant, different team --
# which is the only way to tell a three-column cycle key from a two-column one.
SECOND_TEAM_ID = UUID("00000000-0000-7000-8000-0000000000a3")

MEMBER_ID = UUID("00000000-0000-7000-8000-0000000000e1")
OUTSIDER_ID = UUID("00000000-0000-7000-8000-0000000000e2")

BOOTSTRAP_ISSUE_ID = UUID("00000000-0000-7000-8000-0000000000b1")
OTHER_ISSUE_ID = UUID("00000000-0000-7000-8000-0000000000b2")

BOOTSTRAP_PROJECT_ID = UUID("00000000-0000-7000-8000-0000000000c1")
OTHER_PROJECT_ID = UUID("00000000-0000-7000-8000-0000000000c2")

BOOTSTRAP_LABEL_ID = UUID("00000000-0000-7000-8000-0000000000d1")
OTHER_LABEL_ID = UUID("00000000-0000-7000-8000-0000000000d2")

BOOTSTRAP_CYCLE_ID = UUID("00000000-0000-7000-8000-0000000000f1")
SECOND_TEAM_CYCLE_ID = UUID("00000000-0000-7000-8000-0000000000f2")

TEMPLATE_ID = UUID("00000000-0000-7000-8000-000000000101")

INSERT_USER_SQL = """
INSERT INTO users (id, email, password_hash)
VALUES (
    $1::uuid,
    'user-' || $1::text || '@example.test',
    '$argon2id$not-a-real-hash'
)
"""

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

INSERT_PROJECT_SQL = """
INSERT INTO projects (id, workspace_id, name, state)
VALUES ($1, $2, $3, 'planned')
"""

INSERT_LABEL_SQL = """
INSERT INTO labels (id, workspace_id, name, color)
VALUES ($1, $2, $3, '#aabbcc')
"""

# `$4::int` in the interval arithmetic, not a bare `$4`. PostgreSQL has both
# `integer * interval` and `double precision * interval`, so an untyped
# parameter used as a multiplier AND as the INTEGER `number` column deduces two
# types and fails to parse with "inconsistent types deduced for parameter $4".
INSERT_CYCLE_SQL = """
INSERT INTO cycles (id, workspace_id, team_id, number, name, starts_at, ends_at)
VALUES (
    $1, $2, $3, $4, $5,
    now() + ($4::int * INTERVAL '14 days'),
    now() + (($4::int + 1) * INTERVAL '14 days')
)
"""

INSERT_SUBSCRIBER_SQL = """
INSERT INTO issue_subscribers (workspace_id, issue_id, user_id)
VALUES ($1, $2, $3)
"""

INSERT_TEMPLATE_SQL = """
INSERT INTO issue_templates (
    id, workspace_id, team_id, name, assignee_id, project_id, cycle_id
)
VALUES ($1, $2, $3, $4, $5, $6, $7)
"""

INSERT_TEMPLATE_LABEL_SQL = """
INSERT INTO issue_template_labels (workspace_id, template_id, label_id)
VALUES ($1, $2, $3)
"""

INSERT_NOTIFICATION_SQL = """
INSERT INTO notifications (workspace_id, user_id, actor_id, issue_id, kind)
VALUES ($1, $2, NULL, $3, $4)
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
    """Two workspaces, each with a team, a member, an issue, a project and a
    label -- plus a second team inside the first workspace.

    Symmetric on purpose. Every cross-tenant assertion below is "workspace A's
    row pointing at workspace B's row", and a lopsided fixture -- where one
    side lacks the thing the other is reaching for -- would pass those
    assertions for the wrong reason.

    The second team is the asymmetric part, and it is deliberate: it is what
    makes "another team's cycle, same workspace" expressible at all, which is
    the case a two-column foreign key would accept and this file has to prove
    the three-column one refuses.
    """
    await connection.execute(
        "INSERT INTO workspaces (id, slug, name) VALUES ($1, 'other', 'Other')",
        OTHER_WORKSPACE_ID,
    )

    for team_id, workspace_id, name, key in (
        (OTHER_TEAM_ID, OTHER_WORKSPACE_ID, "Other", "OTH"),
        (SECOND_TEAM_ID, BOOTSTRAP_WORKSPACE_ID, "Platform", "PLT"),
    ):
        await connection.execute(
            "INSERT INTO teams (id, workspace_id, name, key) VALUES ($1, $2, $3, $4)",
            team_id,
            workspace_id,
            name,
            key,
        )

        # 005 seeds workflow states for the teams that exist when it runs, so
        # a team created afterwards has none, and an issue on it cannot be
        # inserted at all.
        await seed_workflow_states(connection, workspace_id, team_id)

    for user_id in (MEMBER_ID, OUTSIDER_ID):
        await connection.execute(INSERT_USER_SQL, user_id)

    for workspace_id, user_id in (
        (BOOTSTRAP_WORKSPACE_ID, MEMBER_ID),
        (OTHER_WORKSPACE_ID, OUTSIDER_ID),
    ):
        await connection.execute(
            "INSERT INTO workspace_members (workspace_id, user_id, role) "
            "VALUES ($1, $2, 'admin')",
            workspace_id,
            user_id,
        )

    for issue_id, workspace_id, team_id, title in (
        (BOOTSTRAP_ISSUE_ID, BOOTSTRAP_WORKSPACE_ID, BOOTSTRAP_TEAM_ID, "Ours"),
        (OTHER_ISSUE_ID, OTHER_WORKSPACE_ID, OTHER_TEAM_ID, "Theirs"),
    ):
        await connection.execute(
            INSERT_ISSUE_SQL, issue_id, workspace_id, team_id, 142, title
        )

    for project_id, workspace_id, name in (
        (BOOTSTRAP_PROJECT_ID, BOOTSTRAP_WORKSPACE_ID, "Launch"),
        (OTHER_PROJECT_ID, OTHER_WORKSPACE_ID, "Their launch"),
    ):
        await connection.execute(INSERT_PROJECT_SQL, project_id, workspace_id, name)

    for label_id, workspace_id, name in (
        (BOOTSTRAP_LABEL_ID, BOOTSTRAP_WORKSPACE_ID, "bug"),
        (OTHER_LABEL_ID, OTHER_WORKSPACE_ID, "their-bug"),
    ):
        await connection.execute(INSERT_LABEL_SQL, label_id, workspace_id, name)

    for cycle_id, team_id, number, name in (
        (BOOTSTRAP_CYCLE_ID, BOOTSTRAP_TEAM_ID, 1, "Core 1"),
        (SECOND_TEAM_CYCLE_ID, SECOND_TEAM_ID, 1, "Platform 1"),
    ):
        await connection.execute(
            INSERT_CYCLE_SQL,
            cycle_id,
            BOOTSTRAP_WORKSPACE_ID,
            team_id,
            number,
            name,
        )

    # The counters, moved past the numbers handed out above. 005 keeps
    # `teams.issue_counter` as the allocator, so a fixture that inserts issues
    # without advancing it leaves the next real create colliding with a number
    # already on the table -- which surfaces as a unique violation in whichever
    # test happens to file the first issue.
    await connection.execute(
        "UPDATE teams SET issue_counter = 142 WHERE id = ANY($1::uuid[])",
        [BOOTSTRAP_TEAM_ID, OTHER_TEAM_ID],
    )


async def insert_bootstrap_template(connection, **overrides) -> None:
    """A template in the bootstrap workspace, with every reference empty."""
    values = {
        "team_id": None,
        "assignee_id": None,
        "project_id": None,
        "cycle_id": None,
    } | overrides

    await connection.execute(
        INSERT_TEMPLATE_SQL,
        TEMPLATE_ID,
        BOOTSTRAP_WORKSPACE_ID,
        values["team_id"],
        "Bug report",
        values["assignee_id"],
        values["project_id"],
        values["cycle_id"],
    )


# --- the ordinary path works ------------------------------------------


async def test_a_member_watches_an_issue_in_their_own_workspace(connection):
    """The feature, before the refusals: this is the row the whole subscriber
    half is about protecting, and it has to be storable or the rest proves
    nothing."""
    await connection.execute(
        INSERT_SUBSCRIBER_SQL,
        BOOTSTRAP_WORKSPACE_ID,
        BOOTSTRAP_ISSUE_ID,
        MEMBER_ID,
    )

    assert (
        await connection.fetchval(
            "SELECT count(*) FROM issue_subscribers WHERE issue_id = $1",
            BOOTSTRAP_ISSUE_ID,
        )
        == 1
    )


async def test_a_template_names_every_kind_of_row_in_its_own_workspace(connection):
    """One template holding a team, a member, a project, a cycle and a label,
    all from the tenant it belongs to.

    Asserted as one row rather than five, because the point is that ONE
    workspace_id satisfies all five composite keys at once -- which is exactly
    what makes the cross-tenant versions below unrepresentable.
    """
    await insert_bootstrap_template(
        connection,
        team_id=BOOTSTRAP_TEAM_ID,
        assignee_id=MEMBER_ID,
        project_id=BOOTSTRAP_PROJECT_ID,
        cycle_id=BOOTSTRAP_CYCLE_ID,
    )
    await connection.execute(
        INSERT_TEMPLATE_LABEL_SQL,
        BOOTSTRAP_WORKSPACE_ID,
        TEMPLATE_ID,
        BOOTSTRAP_LABEL_ID,
    )

    row = await connection.fetchrow(
        "SELECT team_id, assignee_id, project_id, cycle_id FROM issue_templates "
        "WHERE id = $1",
        TEMPLATE_ID,
    )

    assert row["team_id"] == BOOTSTRAP_TEAM_ID
    assert row["assignee_id"] == MEMBER_ID
    assert row["project_id"] == BOOTSTRAP_PROJECT_ID
    assert row["cycle_id"] == BOOTSTRAP_CYCLE_ID


async def test_a_workspace_wide_template_needs_no_team(connection):
    """The nullable `team_id` is a real state, not a missing value.

    A shared "Bug report" is the case it exists for, and forcing a copy per
    team would mean editing six rows to fix one typo.
    """
    await insert_bootstrap_template(connection)

    assert (
        await connection.fetchval(
            "SELECT team_id FROM issue_templates WHERE id = $1", TEMPLATE_ID
        )
        is None
    )


# --- the cross-tenant refusals: subscribers ---------------------------


async def test_a_subscription_cannot_name_another_workspaces_issue(connection):
    """The attack the subscriber half exists to refuse.

    A subscribe mutation carries an issue id, and an id is cheap to guess or to
    copy out of a screenshot. The row here claims to be in the bootstrap
    workspace and points at the other workspace's issue; one workspace_id feeds
    both keys, so the pair simply does not exist.

    Were it storable, the fan-out in `NotificationRepository` would file a
    notification per event onto an issue the watcher cannot read -- an
    exfiltration channel that keeps working long after the request that opened
    it.
    """
    with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
        await connection.execute(
            INSERT_SUBSCRIBER_SQL,
            BOOTSTRAP_WORKSPACE_ID,
            OTHER_ISSUE_ID,
            MEMBER_ID,
        )

    assert raised.value.constraint_name == "issue_subscribers_issue_fk"


async def test_a_non_member_cannot_watch_an_issue(connection):
    """`REFERENCES users (id)` is the obvious spelling and it is the bug.

    The outsider is a real account -- so a single-column key onto `users` would
    accept this row -- and is a member of the OTHER workspace. The composite
    key onto `workspace_members` is what makes "real account" and "may be here"
    different questions.
    """
    with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
        await connection.execute(
            INSERT_SUBSCRIBER_SQL,
            BOOTSTRAP_WORKSPACE_ID,
            BOOTSTRAP_ISSUE_ID,
            OUTSIDER_ID,
        )

    assert raised.value.constraint_name == "issue_subscribers_user_fk"


async def test_a_subscription_cannot_smuggle_a_second_workspace_through_its_own_id(
    connection,
):
    """The other direction: claim to BE the other workspace.

    A row whose workspace_id is the other tenant's, pointing at this tenant's
    issue. Whichever way round an attacker spells the mismatch, one of the two
    constraints is looking at it.
    """
    with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
        await connection.execute(
            INSERT_SUBSCRIBER_SQL,
            OTHER_WORKSPACE_ID,
            BOOTSTRAP_ISSUE_ID,
            OUTSIDER_ID,
        )

    assert raised.value.constraint_name == "issue_subscribers_issue_fk"


async def test_one_person_watches_one_issue_once(connection):
    """The primary key IS the uniqueness rule, and the reason auto-subscribe
    can absorb a duplicate rather than raise inside somebody else's
    transaction."""
    for _ in range(1):
        await connection.execute(
            INSERT_SUBSCRIBER_SQL,
            BOOTSTRAP_WORKSPACE_ID,
            BOOTSTRAP_ISSUE_ID,
            MEMBER_ID,
        )

    with pytest.raises(asyncpg.UniqueViolationError) as raised:
        await connection.execute(
            INSERT_SUBSCRIBER_SQL,
            BOOTSTRAP_WORKSPACE_ID,
            BOOTSTRAP_ISSUE_ID,
            MEMBER_ID,
        )

    assert raised.value.constraint_name == "issue_subscribers_pkey"


async def test_a_re_subscribe_keeps_the_original_watching_since(connection):
    """The behaviour `ON CONFLICT DO NOTHING` buys, asserted against the
    server rather than against the repository's Python.

    "Watching since" must not move when the auto-subscribe path fires again on
    the next comment, or the column stops meaning what its name says.
    """
    await connection.execute(
        INSERT_SUBSCRIBER_SQL,
        BOOTSTRAP_WORKSPACE_ID,
        BOOTSTRAP_ISSUE_ID,
        MEMBER_ID,
    )

    first = await connection.fetchval(
        "SELECT created_at FROM issue_subscribers WHERE issue_id = $1",
        BOOTSTRAP_ISSUE_ID,
    )

    started = await connection.fetchval(
        INSERT_SUBSCRIBER_SQL.replace(
            "VALUES ($1, $2, $3)",
            "VALUES ($1, $2, $3) "
            "ON CONFLICT ON CONSTRAINT issue_subscribers_pkey DO NOTHING "
            "RETURNING user_id",
        ),
        BOOTSTRAP_WORKSPACE_ID,
        BOOTSTRAP_ISSUE_ID,
        MEMBER_ID,
    )

    assert started is None, "the conflict clause suppressed the insert"
    assert (
        await connection.fetchval(
            "SELECT created_at FROM issue_subscribers WHERE issue_id = $1",
            BOOTSTRAP_ISSUE_ID,
        )
        == first
    )


async def test_removing_a_member_does_not_silently_stop_their_watching(connection):
    """RESTRICT, matching notifications_user_fk in 012.

    `RestrictViolationError`, not `ForeignKeyViolationError`. The two are
    siblings rather than one being the other's parent, and PostgreSQL raises
    them for opposite situations: the foreign-key error means a child pointed
    at a parent that was not there, while this one means the parent was there
    and declined to leave. Catching the wrong sibling here would pass for a
    schema with no constraint at all, since the delete would then simply
    succeed and raise nothing.

    This is the same position 012 put `notifications` in, and it is stated
    rather than hidden: the "remove a member" path has to clear these rows
    itself, and today it does not -- so removing a member who watches anything
    is refused. That is a known gap in that path, not a decision this file
    makes on its behalf.
    """
    await connection.execute(
        INSERT_SUBSCRIBER_SQL,
        BOOTSTRAP_WORKSPACE_ID,
        BOOTSTRAP_ISSUE_ID,
        MEMBER_ID,
    )

    with pytest.raises(asyncpg.RestrictViolationError):
        await connection.execute(
            "DELETE FROM workspace_members WHERE workspace_id = $1 AND user_id = $2",
            BOOTSTRAP_WORKSPACE_ID,
            MEMBER_ID,
        )


# --- the cross-tenant refusals: templates -----------------------------


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"team_id": OTHER_TEAM_ID}, "issue_templates_team_fk"),
        ({"assignee_id": OUTSIDER_ID}, "issue_templates_assignee_fk"),
        ({"project_id": OTHER_PROJECT_ID}, "issue_templates_project_fk"),
    ],
    ids=["another team", "another workspace's member", "another project"],
)
async def test_a_template_cannot_name_another_workspaces_row(
    connection, overrides, constraint
):
    """Three references, three composite keys, one workspace_id between them.

    Each of these is a real row in the other tenant, so a single-column key
    onto `teams`, `users` or `projects` would accept every one -- and the
    template would then replay that reference on every apply, months after
    whoever saved it stopped looking.
    """
    with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
        await insert_bootstrap_template(connection, **overrides)

    assert raised.value.constraint_name == constraint


async def test_a_template_cannot_carry_another_workspaces_label(connection):
    """The join table's own key, asserted separately rather than assumed from
    the row above.

    It is a different table with a different pair of constraints, and the
    label side is the easier of the two to declare wrong -- a `label_ids
    UUID[]` column would have carried this id with nothing to notice, which is
    the shape migration 020 rejects in favour of a join.
    """
    await insert_bootstrap_template(connection)

    with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
        await connection.execute(
            INSERT_TEMPLATE_LABEL_SQL,
            BOOTSTRAP_WORKSPACE_ID,
            TEMPLATE_ID,
            OTHER_LABEL_ID,
        )

    assert raised.value.constraint_name == "issue_template_labels_label_fk"


async def test_a_template_cannot_carry_another_teams_cycle(connection):
    """Three columns, not two, and this is the case that tells them apart.

    Both the template's team and the cycle are in the SAME workspace, so a
    two-column key onto (workspace_id, id) would accept this row happily. The
    issue filed from it would then be refused by `issues_cycle_fk` -- which IS
    three columns -- and the template would be one that saves and never works,
    for a reason no error had ever reported.
    """
    with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
        await insert_bootstrap_template(
            connection,
            team_id=BOOTSTRAP_TEAM_ID,
            cycle_id=SECOND_TEAM_CYCLE_ID,
        )

    assert raised.value.constraint_name == "issue_templates_cycle_fk"


async def test_a_workspace_wide_template_cannot_carry_a_cycle(connection):
    """The hole MATCH SIMPLE leaves, closed.

    issue_templates_cycle_fk is skipped entirely for a row with a NULL among
    its three referencing columns, and `team_id` is one of them -- so without
    the CHECK this row would pass the foreign key untested: a workspace-wide
    template pointing at one team's cycle, which no issue filed from it could
    ever satisfy.

    `CheckViolationError` and not the foreign key, which is the assertion: it
    proves the key really was skipped and that something else caught the row.
    """
    with pytest.raises(asyncpg.CheckViolationError) as raised:
        await insert_bootstrap_template(connection, cycle_id=BOOTSTRAP_CYCLE_ID)

    assert raised.value.constraint_name == "issue_templates_cycle_requires_team"


async def test_a_template_label_row_cannot_claim_a_second_workspace(connection):
    """The join from the other direction: a row whose workspace_id is the
    other tenant's, pointing at this tenant's template. One workspace_id feeds
    both keys, so one of them is always looking at the mismatch."""
    await insert_bootstrap_template(connection)

    with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
        await connection.execute(
            INSERT_TEMPLATE_LABEL_SQL,
            OTHER_WORKSPACE_ID,
            TEMPLATE_ID,
            OTHER_LABEL_ID,
        )

    assert raised.value.constraint_name == "issue_template_labels_template_fk"


# --- the bounds a stored, replayed row needs --------------------------


@pytest.mark.parametrize(
    ("column", "value", "constraint"),
    [
        ("name", "", "issue_templates_name_length"),
        ("name", "x" * 101, "issue_templates_name_length"),
        ("title", "x" * 501, "issue_templates_title_length"),
        ("description", "x" * 16385, "issue_templates_description_length"),
    ],
    ids=["empty name", "long name", "long title", "long description"],
)
async def test_a_templates_text_is_bounded(connection, column, value, constraint):
    """A bound, not a validation. Every one of these columns is written by
    whoever may edit a template and echoed back into an issue, so an unbounded
    TEXT column is a way to make a row -- and the response carrying it --
    arbitrarily large."""
    await insert_bootstrap_template(connection)

    with pytest.raises(asyncpg.CheckViolationError) as raised:
        await connection.execute(
            f"UPDATE issue_templates SET {column} = $2 WHERE id = $1",
            TEMPLATE_ID,
            value,
        )

    assert raised.value.constraint_name == constraint


@pytest.mark.parametrize(
    ("column", "value", "constraint"),
    [
        ("priority", 5, "issue_templates_priority_range"),
        ("priority", -1, "issue_templates_priority_range"),
        ("estimate", -1, "issue_templates_estimate_non_negative"),
    ],
)
async def test_a_template_cannot_hold_a_number_an_issue_would_refuse(
    connection, column, value, constraint
):
    """The numeric bounds match `issues_priority_range` in 001 and
    `issues_estimate_non_negative` in 006, so a template that saves is one an
    apply can actually use."""
    await insert_bootstrap_template(connection)

    with pytest.raises(asyncpg.CheckViolationError) as raised:
        await connection.execute(
            f"UPDATE issue_templates SET {column} = $2 WHERE id = $1",
            TEMPLATE_ID,
            value,
        )

    assert raised.value.constraint_name == constraint


async def test_deleting_a_label_a_template_uses_is_refused(connection):
    """RESTRICT, not CASCADE, for the reason 009 gives about project_teams: a
    one-line `DELETE FROM labels` must not strip that label from every
    template in the workspace while reporting `DELETE 1`."""
    await insert_bootstrap_template(connection)
    await connection.execute(
        INSERT_TEMPLATE_LABEL_SQL,
        BOOTSTRAP_WORKSPACE_ID,
        TEMPLATE_ID,
        BOOTSTRAP_LABEL_ID,
    )

    with pytest.raises(asyncpg.RestrictViolationError):
        await connection.execute("DELETE FROM labels WHERE id = $1", BOOTSTRAP_LABEL_ID)


async def test_a_template_carries_each_label_once(connection):
    """The row IS its key, which is what makes a save able to rewrite the set
    by deleting and re-inserting without a position column to keep in step."""
    await insert_bootstrap_template(connection)
    await connection.execute(
        INSERT_TEMPLATE_LABEL_SQL,
        BOOTSTRAP_WORKSPACE_ID,
        TEMPLATE_ID,
        BOOTSTRAP_LABEL_ID,
    )

    with pytest.raises(asyncpg.UniqueViolationError) as raised:
        await connection.execute(
            INSERT_TEMPLATE_LABEL_SQL,
            BOOTSTRAP_WORKSPACE_ID,
            TEMPLATE_ID,
            BOOTSTRAP_LABEL_ID,
        )

    assert raised.value.constraint_name == "issue_template_labels_pkey"


# --- the widened inbox vocabulary -------------------------------------


async def test_the_inbox_admits_a_status_change(connection):
    """The kind 020 adds, and the reason it exists: an assignee can see the
    status on an issue they own, and a watcher asked to follow it precisely so
    they would not have to open it."""
    await connection.execute(
        INSERT_NOTIFICATION_SQL,
        BOOTSTRAP_WORKSPACE_ID,
        MEMBER_ID,
        BOOTSTRAP_ISSUE_ID,
        "status_changed",
    )

    assert (
        await connection.fetchval(
            "SELECT kind FROM notifications WHERE issue_id = $1", BOOTSTRAP_ISSUE_ID
        )
        == "status_changed"
    )


@pytest.mark.parametrize("kind", ["assigned", "commented", "blocked"])
async def test_widening_the_vocabulary_kept_every_kind_012_admitted(connection, kind):
    """A CHECK is widened by dropping and re-adding it, which is also how one
    is accidentally narrowed. Three rows that must still be storable."""
    await connection.execute(
        INSERT_NOTIFICATION_SQL,
        BOOTSTRAP_WORKSPACE_ID,
        MEMBER_ID,
        BOOTSTRAP_ISSUE_ID,
        kind,
    )

    assert (
        await connection.fetchval(
            "SELECT count(*) FROM notifications WHERE kind = $1", kind
        )
        == 1
    )


async def test_the_vocabulary_is_still_closed(connection):
    """Widened, not opened. An open `kind` is how a typo becomes an inbox
    nobody can query: 'state_changed' and 'status_changed' would both store
    fine, and half the notifications would silently stop matching."""
    with pytest.raises(asyncpg.CheckViolationError) as raised:
        await connection.execute(
            INSERT_NOTIFICATION_SQL,
            BOOTSTRAP_WORKSPACE_ID,
            MEMBER_ID,
            BOOTSTRAP_ISSUE_ID,
            "state_changed",
        )

    assert raised.value.constraint_name == "notifications_kind_known"


# --- the fan-out reads the table --------------------------------------


async def test_a_watcher_who_did_not_act_is_notified_and_the_actor_is_not(
    connection,
):
    """`NotificationRepository.notify_about_issue`'s third UNION arm, run
    against the real schema.

    The statement is what migration 020 exists to feed, so it is asserted here
    rather than only against a fake connection: a watcher who is not the
    assignee and not the creator receives the row, and the actor does not --
    which `notifications_actor_is_not_recipient` would refuse anyway, and which
    the `IS DISTINCT FROM` is there to keep from ever being attempted.
    """
    from app.domain.notifications import NotificationKind
    from app.domain.tenancy import WorkspaceScope
    from app.repositories.notifications import NotificationRepository

    # The outsider joins the bootstrap workspace, so there are two members: one
    # to act and one to watch.
    await connection.execute(
        "INSERT INTO workspace_members (workspace_id, user_id, role) "
        "VALUES ($1, $2, 'member')",
        BOOTSTRAP_WORKSPACE_ID,
        OUTSIDER_ID,
    )

    for user_id in (MEMBER_ID, OUTSIDER_ID):
        await connection.execute(
            INSERT_SUBSCRIBER_SQL,
            BOOTSTRAP_WORKSPACE_ID,
            BOOTSTRAP_ISSUE_ID,
            user_id,
        )

    await NotificationRepository().notify_about_issue(
        connection,
        scope=WorkspaceScope(workspace_id=BOOTSTRAP_WORKSPACE_ID),
        issue_id=BOOTSTRAP_ISSUE_ID,
        actor_id=MEMBER_ID,
        kind=NotificationKind.STATUS_CHANGED,
        include_creator=False,
    )

    recipients = await connection.fetch(
        "SELECT user_id FROM notifications WHERE issue_id = $1", BOOTSTRAP_ISSUE_ID
    )

    assert [row["user_id"] for row in recipients] == [OUTSIDER_ID]


async def test_a_watcher_is_notified_once_even_when_they_are_the_assignee(
    connection,
):
    """DISTINCT, and the reason it is not decoration.

    The assignee is auto-subscribed on assignment, so "assignee AND watcher"
    is not an edge case -- it is what every assigned issue looks like. Two
    inbox rows for one event is the bug this would otherwise be.
    """
    from app.domain.notifications import NotificationKind
    from app.domain.tenancy import WorkspaceScope
    from app.repositories.notifications import NotificationRepository

    await connection.execute(
        "UPDATE issues SET assignee_id = $2 WHERE id = $1",
        BOOTSTRAP_ISSUE_ID,
        MEMBER_ID,
    )
    await connection.execute(
        INSERT_SUBSCRIBER_SQL,
        BOOTSTRAP_WORKSPACE_ID,
        BOOTSTRAP_ISSUE_ID,
        MEMBER_ID,
    )

    await NotificationRepository().notify_about_issue(
        connection,
        scope=WorkspaceScope(workspace_id=BOOTSTRAP_WORKSPACE_ID),
        issue_id=BOOTSTRAP_ISSUE_ID,
        actor_id=None,
        kind=NotificationKind.COMMENTED,
        include_creator=True,
    )

    assert (
        await connection.fetchval(
            "SELECT count(*) FROM notifications WHERE issue_id = $1",
            BOOTSTRAP_ISSUE_ID,
        )
        == 1
    )


# --- the statements this schema exists to serve -----------------------


async def test_a_template_round_trips_through_its_repository(connection):
    """Every statement in `TemplateRepository`, against the real server.

    The unit tests above it run on a fake connection, which will happily
    "execute" SQL PostgreSQL would refuse to parse -- and the read here is not
    a plain SELECT: `label_ids` is a correlated `array_agg` subquery, chosen so
    that a menu of twenty templates costs one statement rather than twenty-one.
    A shape that only works in Python is the failure this test exists to catch.

    One test covering create, get, update and delete rather than four, because
    each step's assertion is about the state the previous step left: a
    round-trip is the unit, and four tests would need four fixtures rebuilding
    the same row.
    """
    from app.domain.templates import IssueTemplateDraft
    from app.domain.tenancy import WorkspaceScope
    from app.repositories.templates import TemplateRepository

    repository = TemplateRepository()
    scope = WorkspaceScope(workspace_id=BOOTSTRAP_WORKSPACE_ID)

    created = await repository.create(
        connection,
        scope=scope,
        draft=IssueTemplateDraft(
            name="Bug report",
            team_id=BOOTSTRAP_TEAM_ID,
            title="Bug: ",
            priority=2,
            assignee_id=MEMBER_ID,
            project_id=BOOTSTRAP_PROJECT_ID,
            cycle_id=BOOTSTRAP_CYCLE_ID,
            label_ids=(BOOTSTRAP_LABEL_ID,),
        ),
    )

    assert created.label_ids == (BOOTSTRAP_LABEL_ID,)
    assert created.assignee_id == MEMBER_ID
    assert created.cycle_id == BOOTSTRAP_CYCLE_ID

    # A save REPLACES: the labels, the cycle and the project all go, and the
    # nulls are the whole point -- clearing a default is sending one.
    updated = await repository.update(
        connection,
        scope=scope,
        template_id=created.id,
        draft=IssueTemplateDraft(name="Bug report v2", team_id=BOOTSTRAP_TEAM_ID),
    )

    assert updated is not None
    assert updated.name == "Bug report v2"
    assert updated.label_ids == ()
    assert updated.cycle_id is None
    assert updated.created_at == created.created_at, "a replace is not a re-create"

    # The team listing finds a team template, because the listing is "shared
    # plus this team's".
    listed = await repository.list_for_team(
        connection,
        scope=scope,
        team_id=BOOTSTRAP_TEAM_ID,
        limit=10,
    )

    assert [entity.id for entity in listed] == [created.id]

    assert await repository.delete(connection, scope=scope, template_id=created.id)
    assert await repository.get(connection, scope=scope, template_id=created.id) is None


async def test_another_workspaces_template_is_not_readable_or_deletable(connection):
    """The tenancy of every read, asserted against the server.

    The template is real and belongs to the other tenant. `get` answers None
    and `delete` answers False -- the same answers an id that exists nowhere
    gets -- so nothing downstream ever holds another workspace's defaults to
    re-validate, and no DELETE reaches a row it was not entitled to.
    """
    from app.domain.tenancy import WorkspaceScope
    from app.repositories.templates import TemplateRepository

    await connection.execute(
        INSERT_TEMPLATE_SQL,
        TEMPLATE_ID,
        OTHER_WORKSPACE_ID,
        OTHER_TEAM_ID,
        "Their template",
        None,
        None,
        None,
    )

    repository = TemplateRepository()
    scope = WorkspaceScope(workspace_id=BOOTSTRAP_WORKSPACE_ID)

    assert (
        await repository.get(connection, scope=scope, template_id=TEMPLATE_ID) is None
    )
    assert not await repository.delete(connection, scope=scope, template_id=TEMPLATE_ID)
    assert (
        await connection.fetchval(
            "SELECT count(*) FROM issue_templates WHERE id = $1", TEMPLATE_ID
        )
        == 1
    ), "the other tenant's row is still there"


async def test_a_shared_template_is_listed_for_every_team(connection):
    """The nullable `team_id` read back through the OR the listing uses.

    A shared "Bug report" and a Core-only one: asking as Core sees both, asking
    as Platform sees only the shared one. That is the whole product rule the
    column exists for, and it is one statement rather than two lists merged in
    Python.
    """
    from app.domain.templates import IssueTemplateDraft
    from app.domain.tenancy import WorkspaceScope
    from app.repositories.templates import TemplateRepository

    repository = TemplateRepository()
    scope = WorkspaceScope(workspace_id=BOOTSTRAP_WORKSPACE_ID)

    shared = await repository.create(
        connection, scope=scope, draft=IssueTemplateDraft(name="Alpha shared")
    )
    core_only = await repository.create(
        connection,
        scope=scope,
        draft=IssueTemplateDraft(name="Beta core", team_id=BOOTSTRAP_TEAM_ID),
    )

    core = await repository.list_for_team(
        connection, scope=scope, team_id=BOOTSTRAP_TEAM_ID, limit=10
    )
    platform = await repository.list_for_team(
        connection, scope=scope, team_id=SECOND_TEAM_ID, limit=10
    )
    unscoped = await repository.list_for_team(
        connection, scope=scope, team_id=None, limit=10
    )

    # Ordered by name, which is what the menu index serves.
    assert [entity.id for entity in core] == [shared.id, core_only.id]
    assert [entity.id for entity in platform] == [shared.id]
    assert [entity.id for entity in unscoped] == [shared.id]


# --- applying a template, end to end ----------------------------------


@pytest.fixture
async def templates(postgres_dsn, connection):
    """A real `TemplateService` over the seeded database.

    Real collaborators, not fakes. The unit tests in tests/test_templates.py
    record what `apply` asked for, which proves the ORDER and the SCOPE but
    would go on passing if a keyword were renamed on `IssueService.create` --
    a recording fake accepts anything. This is the one place the two
    signatures actually meet.

    Its own pool alongside the fixture's connection: services acquire, and the
    seeded connection is what the assertions read back through.
    """
    from app.repositories.issue_labels import IssueLabelRepository
    from app.repositories.issues import IssueRepository
    from app.repositories.labels import LabelRepository
    from app.repositories.teams import TeamRepository
    from app.repositories.templates import TemplateRepository
    from app.services.issues import IssueService
    from app.services.labels import LabelService
    from app.services.teams import TeamService
    from app.services.templates import TemplateService

    pool = await asyncpg.create_pool(dsn=postgres_dsn, min_size=1, max_size=4)

    try:
        yield TemplateService(
            pool=pool,
            repository=TemplateRepository(),
            issues=IssueService(
                pool=pool,
                repository=IssueRepository(),
                teams=TeamService(pool=pool, repository=TeamRepository()),
            ),
            labels=LabelService(
                pool=pool,
                repository=LabelRepository(),
                issue_label_repository=IssueLabelRepository(),
            ),
        )
    finally:
        await pool.close()


async def test_applying_a_template_files_an_issue_carrying_every_default(
    connection, templates
):
    """The feature, through the real services, against the real schema.

    Every default the template holds has to survive into the issue, and each
    one arrives by a different route: the title, description, priority,
    estimate and assignee through `IssueService.create`, the project through
    `set_project`, the cycle through `set_cycle`, and the label through
    `LabelService.attach`. A mismatch in any of those four signatures is
    invisible to a recording fake and fatal here.
    """
    from app.domain.templates import IssueTemplateDraft
    from app.domain.tenancy import WorkspaceScope

    scope = WorkspaceScope(workspace_id=BOOTSTRAP_WORKSPACE_ID)

    template = await templates.create(
        scope=scope,
        draft=IssueTemplateDraft(
            name="Bug report",
            team_id=BOOTSTRAP_TEAM_ID,
            title="Bug: something broke",
            description="Steps to reproduce",
            priority=3,
            estimate=5,
            assignee_id=MEMBER_ID,
            project_id=BOOTSTRAP_PROJECT_ID,
            cycle_id=BOOTSTRAP_CYCLE_ID,
            label_ids=(BOOTSTRAP_LABEL_ID,),
        ),
    )

    issue = await templates.apply(
        scope=scope,
        template_id=template.id,
        team_id=BOOTSTRAP_TEAM_ID,
        actor_id=MEMBER_ID,
    )

    row = await connection.fetchrow(
        "SELECT title, description, priority, estimate, assignee_id, creator_id, "
        "project_id, cycle_id FROM issues WHERE id = $1",
        issue.id,
    )

    assert row["title"] == "Bug: something broke"
    assert row["description"] == "Steps to reproduce"
    assert row["priority"] == 3
    assert row["estimate"] == 5
    assert row["assignee_id"] == MEMBER_ID
    assert row["creator_id"] == MEMBER_ID
    assert row["project_id"] == BOOTSTRAP_PROJECT_ID
    assert row["cycle_id"] == BOOTSTRAP_CYCLE_ID

    assert (
        await connection.fetchval(
            "SELECT label_id FROM issue_labels WHERE issue_id = $1", issue.id
        )
        == BOOTSTRAP_LABEL_ID
    )

    # And the assignee is watching it, because being handed an issue is a
    # statement that its future concerns you.
    assert (
        await connection.fetchval(
            "SELECT count(*) FROM issue_subscribers WHERE issue_id = $1 "
            "AND user_id = $2",
            issue.id,
            MEMBER_ID,
        )
        == 1
    )


async def test_applying_another_workspaces_template_is_not_found(connection, templates):
    """The tenancy of the apply path, through the service rather than the
    repository.

    The template is real and belongs to the other tenant. `apply` reads it
    under the scope the resolver authorized, so it resolves to nothing -- and
    no issue is filed, which is the assertion that matters: a path that read
    first and checked later would have created the row before noticing.
    """
    from app.domain.errors import ValidationError
    from app.domain.tenancy import WorkspaceScope

    await connection.execute(
        INSERT_TEMPLATE_SQL,
        TEMPLATE_ID,
        OTHER_WORKSPACE_ID,
        OTHER_TEAM_ID,
        "Their template",
        None,
        None,
        None,
    )

    before = await connection.fetchval("SELECT count(*) FROM issues")

    with pytest.raises(ValidationError) as raised:
        await templates.apply(
            scope=WorkspaceScope(workspace_id=BOOTSTRAP_WORKSPACE_ID),
            template_id=TEMPLATE_ID,
            team_id=BOOTSTRAP_TEAM_ID,
            actor_id=MEMBER_ID,
        )

    assert [(issue.field, issue.code) for issue in raised.value.issues] == [
        ("templateId", "NOT_FOUND")
    ]
    assert await connection.fetchval("SELECT count(*) FROM issues") == before
