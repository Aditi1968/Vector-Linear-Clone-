"""Migration 022 applied by the real runner, then asked what it built.

022 adds four things that all point at rows a client names by id: initiatives,
the projects inside them, the updates posted against both, and the dependencies
between projects. Every one of those ids arrives from a browser, so every one
of them is a chance to reach into another tenant -- and the answer has to be
the database, not the service. A resolver that scopes its lookup correctly is
right until someone writes a second one, and the second one is where this class
of bug lives.

So most of the assertions below are about rows PostgreSQL will not store:

  * a project from another workspace joined to this workspace's initiative --
    the requirement the whole file is shaped around;
  * an initiative parented to another workspace's initiative;
  * an update posted by somebody who is not a member here, which is the
    impersonation a single-column key to `users (id)` would accept;
  * a project blocking another workspace's project, in both spellings;
  * a project blocking itself.

Three more are about the shape being useful rather than safe: the four health
CHECKs admit exactly the same three values (so `projects.health` and the update
log cannot drift into different vocabularies), a project link and an initiative
parent are RESTRICT rather than CASCADE, and the ordinary paths work at all --
without which the refusals prove nothing.

What is NOT here, deliberately: cycles beyond one step, in either graph. No
CHECK constraint can see a second row, so `initiatives_parent_not_self` and
`project_dependencies_not_self` are all the database can refuse and all that is
asserted here. The multi-step guards are the services' and are tested in
tests/test_initiatives.py, which is where they live.

Marked `db`: deselected by default, skipped when Docker is unreachable.
"""

from uuid import UUID

import asyncpg
import pytest

from app.domain.health import HEALTH_VALUES
from app.domain.initiatives import INITIATIVE_STATUSES

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

# The other tenant: a real workspace, with a real member, a real project and a
# real initiative, whose rows every cross-tenant assertion below tries and fails
# to reach. It has to be real -- a nonexistent id would be refused by any
# spelling of these constraints and would prove nothing about which one is in
# force.
OTHER_WORKSPACE_ID = UUID("00000000-0000-7000-8000-0000000000a1")
OTHER_TEAM_ID = UUID("00000000-0000-7000-8000-0000000000a2")

MEMBER_ID = UUID("00000000-0000-7000-8000-0000000000e1")
OUTSIDER_ID = UUID("00000000-0000-7000-8000-0000000000e2")

PROJECT_ID = UUID("00000000-0000-7000-8000-0000000000c1")
SECOND_PROJECT_ID = UUID("00000000-0000-7000-8000-0000000000c2")
OTHER_PROJECT_ID = UUID("00000000-0000-7000-8000-0000000000c3")

INITIATIVE_ID = UUID("00000000-0000-7000-8000-0000000000d1")
CHILD_INITIATIVE_ID = UUID("00000000-0000-7000-8000-0000000000d2")
OTHER_INITIATIVE_ID = UUID("00000000-0000-7000-8000-0000000000d3")

INSERT_USER_SQL = """
INSERT INTO users (id, email, password_hash)
VALUES (
    $1::uuid,
    'user-' || $1::text || '@example.test',
    '$argon2id$not-a-real-hash'
)
"""

INSERT_PROJECT_SQL = """
INSERT INTO projects (id, workspace_id, name, state)
VALUES ($1, $2, $3, 'planned')
"""

INSERT_INITIATIVE_SQL = """
INSERT INTO initiatives (id, workspace_id, name, status)
VALUES ($1, $2, $3, 'planned')
"""

INSERT_LINK_SQL = """
INSERT INTO initiative_projects (workspace_id, initiative_id, project_id)
VALUES ($1, $2, $3)
"""

INSERT_PROJECT_UPDATE_SQL = """
INSERT INTO project_updates (workspace_id, project_id, health, body, author_id)
VALUES ($1, $2, $3, $4, $5)
"""

INSERT_INITIATIVE_UPDATE_SQL = """
INSERT INTO initiative_updates
    (workspace_id, initiative_id, health, body, author_id)
VALUES ($1, $2, $3, $4, $5)
"""

INSERT_DEPENDENCY_SQL = """
INSERT INTO project_dependencies
    (workspace_id, blocking_project_id, blocked_project_id)
VALUES ($1, $2, $3)
"""

# The values a CHECK admits, read back out of the catalog rather than out of the
# file. `pg_get_constraintdef` renders what the server actually enforces, which
# is the thing a drifting constant would disagree with.
CONSTRAINT_DEFINITION_SQL = """
SELECT pg_get_constraintdef(oid)
FROM pg_constraint
WHERE conname = $1
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
    """Two workspaces, each with a team, a member, a project and an initiative.

    Symmetric on purpose. Every cross-tenant assertion below is "workspace A's
    row pointing at workspace B's row", and a lopsided fixture -- where one side
    lacks the thing the other is reaching for -- would pass those assertions for
    the wrong reason.
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
    # team created above -- afterwards -- has none. Nothing in this file needs
    # an issue, but the helper keeps the second tenant a real one rather than a
    # half-built stand-in.
    await seed_workflow_states(connection, OTHER_WORKSPACE_ID, OTHER_TEAM_ID)

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

    for project_id, workspace_id, name in (
        (PROJECT_ID, BOOTSTRAP_WORKSPACE_ID, "Ours"),
        (SECOND_PROJECT_ID, BOOTSTRAP_WORKSPACE_ID, "Ours too"),
        (OTHER_PROJECT_ID, OTHER_WORKSPACE_ID, "Theirs"),
    ):
        await connection.execute(INSERT_PROJECT_SQL, project_id, workspace_id, name)

    for initiative_id, workspace_id, name in (
        (INITIATIVE_ID, BOOTSTRAP_WORKSPACE_ID, "Ship v2"),
        (CHILD_INITIATIVE_ID, BOOTSTRAP_WORKSPACE_ID, "Ship the API"),
        (OTHER_INITIATIVE_ID, OTHER_WORKSPACE_ID, "Their goal"),
    ):
        await connection.execute(
            INSERT_INITIATIVE_SQL, initiative_id, workspace_id, name
        )


# --- the ordinary paths work ------------------------------------------


async def test_a_project_joins_an_initiative_in_its_own_workspace(connection):
    """The feature, before the refusals: this is the row the whole file is
    about protecting, and it has to be storable or the rest proves nothing."""
    await connection.execute(
        INSERT_LINK_SQL, BOOTSTRAP_WORKSPACE_ID, INITIATIVE_ID, PROJECT_ID
    )

    assert (
        await connection.fetchval(
            "SELECT count(*) FROM initiative_projects WHERE initiative_id = $1",
            INITIATIVE_ID,
        )
        == 1
    )


async def test_an_initiative_nests_under_another_in_its_own_workspace(connection):
    await connection.execute(
        "UPDATE initiatives SET parent_initiative_id = $2 WHERE id = $1",
        CHILD_INITIATIVE_ID,
        INITIATIVE_ID,
    )

    assert (
        await connection.fetchval(
            "SELECT parent_initiative_id FROM initiatives WHERE id = $1",
            CHILD_INITIATIVE_ID,
        )
        == INITIATIVE_ID
    )


async def test_a_project_starts_with_no_health_at_all(connection):
    """NULL is a real state and not a gap.

    A project nobody has posted about has no health, which is a different fact
    from one reported as on track -- and it is what stops a board showing a
    wall of green for a workspace nobody is updating. The column is therefore
    nullable and carries no DEFAULT.
    """
    assert (
        await connection.fetchval(
            "SELECT health FROM projects WHERE id = $1", PROJECT_ID
        )
        is None
    )


async def test_an_update_is_storable_by_a_member_of_the_same_workspace(connection):
    await connection.execute(
        INSERT_PROJECT_UPDATE_SQL,
        BOOTSTRAP_WORKSPACE_ID,
        PROJECT_ID,
        "at_risk",
        "Slipping a week on the migration.",
        MEMBER_ID,
    )

    assert (
        await connection.fetchval(
            "SELECT health FROM project_updates WHERE project_id = $1", PROJECT_ID
        )
        == "at_risk"
    )


async def test_one_project_blocks_another_in_its_own_workspace(connection):
    await connection.execute(
        INSERT_DEPENDENCY_SQL,
        BOOTSTRAP_WORKSPACE_ID,
        PROJECT_ID,
        SECOND_PROJECT_ID,
    )

    assert (
        await connection.fetchval(
            "SELECT count(*) FROM project_dependencies WHERE blocking_project_id = $1",
            PROJECT_ID,
        )
        == 1
    )


# --- the cross-tenant refusals ----------------------------------------


async def test_an_initiative_cannot_hold_another_workspaces_project(connection):
    """The attack this migration is shaped around.

    The row claims to be in the bootstrap workspace and names the other
    workspace's project. `initiative_projects_project_fk` reads the same
    workspace_id as the initiative key does, so the pair simply does not exist
    -- there is no column for the second workspace to go in.
    """
    with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
        await connection.execute(
            INSERT_LINK_SQL,
            BOOTSTRAP_WORKSPACE_ID,
            INITIATIVE_ID,
            OTHER_PROJECT_ID,
        )

    assert raised.value.constraint_name == "initiative_projects_project_fk"


async def test_a_link_row_cannot_smuggle_a_second_workspace_through_its_own_id(
    connection,
):
    """The other direction: claim to BE the other workspace.

    A row whose workspace_id is the other tenant's, naming this tenant's
    initiative. It fails on the initiative key rather than the project key,
    which is the point -- both halves are checked against the one column, so
    whichever way round an attacker spells the mismatch, one of the two
    constraints is looking at it.
    """
    with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
        await connection.execute(
            INSERT_LINK_SQL,
            OTHER_WORKSPACE_ID,
            INITIATIVE_ID,
            OTHER_PROJECT_ID,
        )

    assert raised.value.constraint_name == "initiative_projects_initiative_fk"


async def test_an_initiative_cannot_be_parented_across_workspaces(connection):
    """The hierarchy is tenant-local, and the composite key is what says so.

    A single-column reference to `initiatives (id)` would accept this and read
    perfectly well; the tree would then span two tenants, and every constraint
    in the database would report success.
    """
    with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
        await connection.execute(
            "UPDATE initiatives SET parent_initiative_id = $2 WHERE id = $1",
            CHILD_INITIATIVE_ID,
            OTHER_INITIATIVE_ID,
        )

    assert raised.value.constraint_name == "initiatives_parent_fk"


async def test_an_initiative_cannot_be_owned_by_a_non_member(connection):
    """`REFERENCES users (id)` is the obvious spelling and it is the bug.

    The outsider is a real account -- it has to be, or this would pass against
    any spelling -- and belongs to the other workspace. A single-column key
    would accept it as the owner of this workspace's initiative.
    """
    with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
        await connection.execute(
            "UPDATE initiatives SET owner_id = $2 WHERE id = $1",
            INITIATIVE_ID,
            OUTSIDER_ID,
        )

    assert raised.value.constraint_name == "initiatives_owner_fk"


@pytest.mark.parametrize(
    ("statement", "constraint"),
    [
        (INSERT_PROJECT_UPDATE_SQL, "project_updates_author_fk"),
        (INSERT_INITIATIVE_UPDATE_SQL, "initiative_updates_author_fk"),
    ],
)
async def test_an_update_cannot_be_attributed_to_a_non_member(
    connection, statement, constraint
):
    """The impersonation a single-column author key would accept.

    Asserted for both tables rather than inferred from one: they are two
    constraints, and the second is exactly the kind that gets declared wrong
    because the first one already reads correct.
    """
    subject = PROJECT_ID if "project" in constraint else INITIATIVE_ID

    with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
        await connection.execute(
            statement,
            BOOTSTRAP_WORKSPACE_ID,
            subject,
            "on_track",
            "Looks fine from over here.",
            OUTSIDER_ID,
        )

    assert raised.value.constraint_name == constraint


async def test_a_project_cannot_block_another_workspaces_project(connection):
    with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
        await connection.execute(
            INSERT_DEPENDENCY_SQL,
            BOOTSTRAP_WORKSPACE_ID,
            PROJECT_ID,
            OTHER_PROJECT_ID,
        )

    assert raised.value.constraint_name == "project_dependencies_blocked_fk"


async def test_a_project_cannot_be_blocked_by_another_workspaces_project(connection):
    """The same mismatch spelled the other way round, so that the constraint
    that catches it is the other one of the pair. Both foreign keys read the
    row's single workspace_id, so neither direction has anywhere to hide."""
    with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
        await connection.execute(
            INSERT_DEPENDENCY_SQL,
            BOOTSTRAP_WORKSPACE_ID,
            OTHER_PROJECT_ID,
            PROJECT_ID,
        )

    assert raised.value.constraint_name == "project_dependencies_blocking_fk"


# --- the one-step loops the database can refuse -----------------------


async def test_a_project_cannot_block_itself(connection):
    """One step is all a CHECK can see, and this is it.

    Longer loops are the service's -- no CHECK may read a second row -- and
    are asserted in tests/test_initiatives.py. This one is here because it is
    the half the database really does hold, whatever the application forgets.
    """
    with pytest.raises(asyncpg.CheckViolationError) as raised:
        await connection.execute(
            INSERT_DEPENDENCY_SQL,
            BOOTSTRAP_WORKSPACE_ID,
            PROJECT_ID,
            PROJECT_ID,
        )

    assert raised.value.constraint_name == "project_dependencies_not_self"


async def test_an_initiative_cannot_be_its_own_parent(connection):
    with pytest.raises(asyncpg.CheckViolationError) as raised:
        await connection.execute(
            "UPDATE initiatives SET parent_initiative_id = id WHERE id = $1",
            INITIATIVE_ID,
        )

    assert raised.value.constraint_name == "initiatives_parent_not_self"


async def test_an_initiative_with_no_parent_is_exempt_rather_than_refused(
    connection,
):
    """The MATCH SIMPLE case, asserted rather than assumed.

    `initiatives_parent_fk` is skipped entirely for a row with a NULL among its
    referencing columns, and because `workspace_id` is NOT NULL the only such
    column is the parent -- so the exemption is exactly "this initiative has no
    parent". If that were not true, no top-level initiative could exist at all,
    and the seed above would already have failed.
    """
    assert (
        await connection.fetchval(
            "SELECT count(*) FROM initiatives "
            "WHERE workspace_id = $1 AND parent_initiative_id IS NULL",
            BOOTSTRAP_WORKSPACE_ID,
        )
        == 2
    )


# --- duplicates -------------------------------------------------------


async def test_a_project_joins_an_initiative_only_once(connection):
    """The primary key IS the uniqueness rule; there is no surrogate id for a
    second spelling of the same membership to hide behind."""
    await connection.execute(
        INSERT_LINK_SQL, BOOTSTRAP_WORKSPACE_ID, INITIATIVE_ID, PROJECT_ID
    )

    with pytest.raises(asyncpg.UniqueViolationError) as raised:
        await connection.execute(
            INSERT_LINK_SQL, BOOTSTRAP_WORKSPACE_ID, INITIATIVE_ID, PROJECT_ID
        )

    assert raised.value.constraint_name == "initiative_projects_pkey"


async def test_a_dependency_is_recorded_once_but_both_directions_are_storable(
    connection,
):
    """One row per edge, and the reverse edge is a DIFFERENT edge.

    Unlike `issue_relations`, this edge has no symmetric counterpart to
    canonicalise, so (A blocks B) and (B blocks A) are two rows the schema
    admits -- a mutual deadlock, which is a product judgement rather than an
    integrity violation, and which the service's cycle walk is what refuses.
    Asserted so that a future canonicalising constraint has to be a deliberate
    change rather than a silent one.
    """
    await connection.execute(
        INSERT_DEPENDENCY_SQL, BOOTSTRAP_WORKSPACE_ID, PROJECT_ID, SECOND_PROJECT_ID
    )

    with pytest.raises(asyncpg.UniqueViolationError) as raised:
        await connection.execute(
            INSERT_DEPENDENCY_SQL,
            BOOTSTRAP_WORKSPACE_ID,
            PROJECT_ID,
            SECOND_PROJECT_ID,
        )

    assert raised.value.constraint_name == "project_dependencies_pkey"

    await connection.execute(
        INSERT_DEPENDENCY_SQL, BOOTSTRAP_WORKSPACE_ID, SECOND_PROJECT_ID, PROJECT_ID
    )

    assert await connection.fetchval("SELECT count(*) FROM project_dependencies") == 2


# --- the vocabularies agree -------------------------------------------


@pytest.mark.parametrize(
    "constraint",
    [
        "projects_health_check",
        "initiatives_health_check",
        "project_updates_health_check",
        "initiative_updates_health_check",
    ],
)
async def test_every_health_constraint_admits_the_same_three_values(
    connection, constraint
):
    """Four constraints and one Python tuple, all statements of one rule.

    022 writes the list out four times rather than sharing it, because there is
    no way to share a CHECK expression between two tables short of a domain
    type or a function -- and both put the vocabulary somewhere a reader of
    either table cannot see it. The cost of that choice is that they can drift,
    so this is the test that says they have not: the definitions are read back
    out of the catalog, which is what the server actually enforces.
    """
    definition = await connection.fetchval(CONSTRAINT_DEFINITION_SQL, constraint)

    assert definition is not None, f"{constraint} does not exist"

    for value in HEALTH_VALUES:
        assert f"'{value}'" in definition

    # And nothing else: a fourth value admitted by one constraint and not the
    # others is exactly the drift this asserts against, and counting is what
    # catches an ADDITION rather than only a removal.
    assert definition.count("'") == 2 * len(HEALTH_VALUES)


async def test_the_status_constraint_and_the_domain_tuple_agree(connection):
    """`initiatives_status_check` and INITIATIVE_STATUSES are two statements of
    one rule, and the service refuses an unknown status without a round trip
    precisely because it trusts its own copy."""
    definition = await connection.fetchval(
        CONSTRAINT_DEFINITION_SQL, "initiatives_status_check"
    )

    for status in INITIATIVE_STATUSES:
        assert f"'{status}'" in definition

    assert definition.count("'") == 2 * len(INITIATIVE_STATUSES)


async def test_an_invented_health_is_refused(connection):
    with pytest.raises(asyncpg.CheckViolationError) as raised:
        await connection.execute(
            INSERT_PROJECT_UPDATE_SQL,
            BOOTSTRAP_WORKSPACE_ID,
            PROJECT_ID,
            "probably_fine",
            "Should be OK.",
            MEMBER_ID,
        )

    assert raised.value.constraint_name == "project_updates_health_check"


async def test_an_empty_update_body_is_refused(connection):
    """A bound at both ends. The upper one stops a single row being megabytes
    wide; the lower one stops an update that reports a health and says nothing
    -- which is a row the history cannot be read from."""
    with pytest.raises(asyncpg.CheckViolationError) as raised:
        await connection.execute(
            INSERT_PROJECT_UPDATE_SQL,
            BOOTSTRAP_WORKSPACE_ID,
            PROJECT_ID,
            "on_track",
            "",
            MEMBER_ID,
        )

    assert raised.value.constraint_name == "project_updates_body_length"


# --- what a delete must not silently do -------------------------------


async def test_deleting_a_project_does_not_silently_leave_its_initiatives(
    connection,
):
    """RESTRICT, not CASCADE, for the reason 009 gives about project_teams: a
    one-line delete must not discard rows in another table while reporting
    `DELETE 1`. ProjectService removes them itself, in order, in the same
    transaction.

    `RestrictViolationError`, not `ForeignKeyViolationError`. The two are
    siblings under IntegrityConstraintViolationError rather than one being the
    other's parent, and PostgreSQL raises them for opposite situations: the
    foreign-key error means a child pointed at a parent that was not there,
    while this one means the parent was there and declined to leave. Catching
    the wrong sibling here would pass for a schema that had no constraint at
    all, since the delete would then simply succeed and raise nothing.
    """
    await connection.execute(
        INSERT_LINK_SQL, BOOTSTRAP_WORKSPACE_ID, INITIATIVE_ID, PROJECT_ID
    )

    with pytest.raises(asyncpg.RestrictViolationError):
        await connection.execute("DELETE FROM projects WHERE id = $1", PROJECT_ID)


async def test_deleting_an_initiative_does_not_silently_delete_its_children(
    connection,
):
    """The self-reference is the one CASCADE would be most destructive on: it
    would delete the entire tree beneath a goal, recursively, while the command
    tag read `DELETE 1`. InitiativeService promotes the children first, which
    is the product decision RESTRICT forces somebody to make out loud."""
    await connection.execute(
        "UPDATE initiatives SET parent_initiative_id = $2 WHERE id = $1",
        CHILD_INITIATIVE_ID,
        INITIATIVE_ID,
    )

    with pytest.raises(asyncpg.RestrictViolationError):
        await connection.execute("DELETE FROM initiatives WHERE id = $1", INITIATIVE_ID)


async def test_removing_a_member_who_posted_an_update_is_refused(connection):
    """A member who has reported on a project cannot be removed until their
    updates are reassigned or deleted. That is the correct refusal to make
    loudly; CASCADE would destroy a project's history as a side effect of an
    administrative removal."""
    await connection.execute(
        INSERT_PROJECT_UPDATE_SQL,
        BOOTSTRAP_WORKSPACE_ID,
        PROJECT_ID,
        "on_track",
        "All good.",
        MEMBER_ID,
    )

    with pytest.raises(asyncpg.RestrictViolationError):
        await connection.execute(
            "DELETE FROM workspace_members WHERE workspace_id = $1 AND user_id = $2",
            BOOTSTRAP_WORKSPACE_ID,
            MEMBER_ID,
        )
