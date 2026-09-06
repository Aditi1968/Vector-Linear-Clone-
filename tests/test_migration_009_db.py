"""Migration 009 applied by the real runner, then asked what it built.

Most of what this migration does is ordinary and would survive a careless
rewrite unnoticed. Four things would not, and each is one word away from a
version that looks right in a diff and is wrong in production:

  * `lead_id UUID REFERENCES users (id)` is the natural spelling and it grants
    every user in the system the right to lead every workspace's projects. The
    reference has to be the composite `(workspace_id, lead_id) ->
    workspace_members (workspace_id, user_id)`, which is a pair rather than an
    id, so the database itself refuses a lead who is not a member of the
    project's own workspace. That is the point of this file.
  * a pair of single-column foreign keys on `project_teams` accepts workspace
    A's project alongside workspace B's team, and neither constraint notices;
  * `issues_milestone_fk` over two columns rather than three accepts a
    milestone belonging to a different project in the same workspace;
  * `ON DELETE CASCADE` and `ON DELETE RESTRICT` differ by whether deleting a
    workspace member silently vacates the projects they lead.

None of the four is visible in a catalog listing that does not print a key's
columns, so this file asks the server both ways: it reads the catalog for the
shape, and it writes rows to find out what the shape actually refuses.

Two disciplines are borrowed from tests/test_migration_004_db.py: action bytes
are pinned as bytes rather than read out of `pg_get_constraintdef`, because
the byte is what the executor consults; and expected values are written as
literals rather than fetched from the database being checked.

Marked `db`: deselected by default, skipped when Docker is unreachable.
"""

from uuid import UUID

import asyncpg
import pytest

from app.domain.projects import PROJECT_STATES

from tests.conftest import apply_all_migrations, reset_schema


pytestmark = pytest.mark.db

# The tenant 002 seeds, named there as literals precisely so a test can assert
# against a constant instead of querying for the value it is about to check.
BOOTSTRAP_WORKSPACE_ID = UUID("00000000-0000-7000-8000-000000000001")
BOOTSTRAP_TEAM_ID = UUID("00000000-0000-7000-8000-000000000002")

OTHER_WORKSPACE_ID = UUID("00000000-0000-7000-8000-0000000000a1")
OTHER_TEAM_ID = UUID("00000000-0000-7000-8000-0000000000a2")

# A member of the bootstrap workspace: the one user who may lead its projects.
MEMBER_ID = UUID("00000000-0000-7000-8000-0000000000e1")

# A real account that belongs to the OTHER workspace. This is the id the naive
# `REFERENCES users (id)` would accept as a lead of a bootstrap project, and
# the composite key refuses. It has to be a real user with a real membership,
# because a nonexistent id would be refused by either spelling and would prove
# nothing about which one is in force.
OUTSIDER_ID = UUID("00000000-0000-7000-8000-0000000000e2")

# An account belonging to no workspace at all.
UNAFFILIATED_ID = UUID("00000000-0000-7000-8000-0000000000e3")

PROJECTS_TABLE = "public.projects"

# The single-byte codes pg_constraint stores for referential actions, pinned
# as bytes for the reason tests/test_migration_002_db.py gives: the byte is
# what the executor consults, and pinning it stops RESTRICT drifting into
# CASCADE behind a rendering that still reads plausibly.
RESTRICT = "r"

# MATCH SIMPLE, which is what a foreign key declared without a MATCH clause
# gets. It is load-bearing here rather than incidental: it is what exempts a
# row whose `lead_id` is NULL from projects_lead_fk, and therefore what makes
# "this project has no lead" a storable state.
MATCH_SIMPLE = "s"

INSERT_USER_SQL = """
INSERT INTO users (id, email, password_hash)
VALUES (
    $1::uuid,
    'user-' || $1::text || '@example.test',
    '$argon2id$not-a-real-hash'
)
"""

INSERT_PROJECT_SQL = """
INSERT INTO projects (workspace_id, name, state, lead_id)
VALUES ($1, $2, $3, $4)
RETURNING id
"""

# Columns and referenced columns of one foreign key, in key order.
#
# `confrelid::regclass` names what the key points at, which is what catches a
# constraint that restricts correctly against the wrong table. The two column
# arrays are what catch the single-column version of the same constraint --
# the failure that no rendering of the table makes obvious.
FOREIGN_KEY_SQL = """
SELECT
    confrelid::regclass::text AS referenced_table,
    confupdtype::text AS update_action,
    confdeltype::text AS delete_action,
    confmatchtype::text AS match_type,
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

INDEX_COLUMNS_SQL = """
SELECT pg_attribute.attname AS column_name
FROM pg_index
JOIN pg_class ON pg_class.oid = pg_index.indexrelid
CROSS JOIN LATERAL
    unnest(pg_index.indkey::smallint[]) WITH ORDINALITY AS key_column(attnum, ord)
LEFT JOIN pg_attribute
    ON pg_attribute.attrelid = pg_index.indrelid
    AND pg_attribute.attnum = key_column.attnum
WHERE pg_index.indrelid = $1::regclass AND pg_class.relname = $2
ORDER BY key_column.ord
"""

# The states the CHECK must reject. The capitalisations are in the list on
# purpose: `IN` is case-sensitive, so 'Planned' is a different string, and a
# constraint that admitted it would let one state be spelled two ways.
REJECTED_STATES = ("", "done", "in_progress", "Planned", "PLANNED", "planned ")


@pytest.fixture
async def applied(postgres_dsn):
    """The whole migration chain, then two tenants and three accounts.

    Every table is dropped first because the container is shared for the whole
    session, so a `workspaces` left behind by another file would make 002's
    CREATE TABLE fail here for reasons unrelated to this migration.

    Applied through `scripts.apply_migration`, exactly as an operator would --
    `apply_all_migrations` is the fixture that does it -- rather than by
    executing the file's text. A hand-run gets no ledger row and no advisory
    lock, and this file is partly about the runner accepting 009 at all.
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
        await connection.execute(
            "INSERT INTO teams (id, workspace_id, name, key) VALUES ($1, $2, $3, $4)",
            OTHER_TEAM_ID,
            OTHER_WORKSPACE_ID,
            "Acme Core",
            "ACME",
        )

        for user_id in (MEMBER_ID, OUTSIDER_ID, UNAFFILIATED_ID):
            await connection.execute(INSERT_USER_SQL, user_id)

        # The memberships that decide who may lead what. UNAFFILIATED_ID
        # deliberately gets none.
        await connection.execute(
            "INSERT INTO workspace_members (workspace_id, user_id, role) "
            "VALUES ($1, $2, $3)",
            BOOTSTRAP_WORKSPACE_ID,
            MEMBER_ID,
            "member",
        )
        await connection.execute(
            "INSERT INTO workspace_members (workspace_id, user_id, role) "
            "VALUES ($1, $2, $3)",
            OTHER_WORKSPACE_ID,
            OUTSIDER_ID,
            "owner",
        )

        yield connection
    finally:
        await connection.close()


# --------------------------------------------------------------- the lead


async def test_the_lead_reference_is_the_membership_pair_and_not_the_user(applied):
    """The constraint's shape, read from the catalog.

    This is the assertion that fails if `projects_lead_fk` is ever "simplified"
    to `REFERENCES users (id)`. That rewrite passes every behavioural test
    below that uses an id belonging to nobody, because a nonexistent user is
    refused either way -- so the shape is worth pinning directly, alongside the
    behaviour, rather than trusting one to imply the other.

    MATCH SIMPLE is asserted too. It is what exempts a NULL `lead_id` from the
    check, and therefore what makes a project with no lead storable at all; a
    change to MATCH FULL would refuse every leaderless project, and the test
    that catches it is this one plus `test_a_project_may_have_no_lead`.
    """
    row = await applied.fetchrow(FOREIGN_KEY_SQL, "projects_lead_fk")

    assert row is not None, "projects_lead_fk does not exist"

    assert row["referenced_table"] == "workspace_members"
    assert list(row["referencing_columns"]) == ["workspace_id", "lead_id"]
    assert list(row["referenced_columns"]) == ["workspace_id", "user_id"]
    assert row["match_type"] == MATCH_SIMPLE

    # RESTRICT both ways: removing a member who leads a project is refused
    # rather than silently vacating the projects they lead.
    assert row["delete_action"] == RESTRICT
    assert row["update_action"] == RESTRICT


async def test_a_member_of_the_projects_own_workspace_may_lead_it(applied):
    """The permitted case, so the refusals below are not passing vacuously."""
    project_id = await applied.fetchval(
        INSERT_PROJECT_SQL,
        BOOTSTRAP_WORKSPACE_ID,
        "Launch",
        "planned",
        MEMBER_ID,
    )

    assert project_id is not None

    stored = await applied.fetchval(
        "SELECT lead_id FROM projects WHERE id = $1", project_id
    )

    assert stored == MEMBER_ID


async def test_a_member_of_another_workspace_may_not_lead_this_ones_project(applied):
    """The trap this migration exists to close.

    OUTSIDER_ID is a real account with a real, current membership -- of the
    OTHER workspace. `REFERENCES users (id)` would accept it without a
    complaint, because it is a real user; so would any check that asked only
    whether the id names an account. Only the composite key notices that the
    membership it names is in the wrong tenant.
    """
    with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
        await applied.fetchval(
            INSERT_PROJECT_SQL,
            BOOTSTRAP_WORKSPACE_ID,
            "Launch",
            "planned",
            OUTSIDER_ID,
        )

    assert raised.value.constraint_name == "projects_lead_fk"


async def test_a_user_who_belongs_to_no_workspace_may_not_lead_a_project(applied):
    """A real account with no membership anywhere is refused just the same."""
    with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
        await applied.fetchval(
            INSERT_PROJECT_SQL,
            BOOTSTRAP_WORKSPACE_ID,
            "Launch",
            "planned",
            UNAFFILIATED_ID,
        )

    assert raised.value.constraint_name == "projects_lead_fk"


async def test_a_project_may_have_no_lead(applied):
    """NULL is a state, not a missing value -- and MATCH SIMPLE is what allows it."""
    project_id = await applied.fetchval(
        INSERT_PROJECT_SQL,
        BOOTSTRAP_WORKSPACE_ID,
        "Unclaimed",
        "planned",
        None,
    )

    assert project_id is not None


async def test_the_lead_may_be_taken_off_an_existing_project(applied):
    """Clearing the lead writes a NULL rather than being refused by the key."""
    project_id = await applied.fetchval(
        INSERT_PROJECT_SQL,
        BOOTSTRAP_WORKSPACE_ID,
        "Launch",
        "planned",
        MEMBER_ID,
    )

    await applied.execute(
        "UPDATE projects SET lead_id = NULL WHERE id = $1", project_id
    )

    assert (
        await applied.fetchval("SELECT lead_id FROM projects WHERE id = $1", project_id)
    ) is None


async def test_an_existing_project_cannot_be_reassigned_to_a_non_member(applied):
    """The key is re-checked on UPDATE, not only on INSERT.

    A project created with a legitimate lead must not become a way to smuggle
    one in afterwards. `workspace_id` is not touched by the statement, so the
    pair the key checks is the row's own tenant against the supplied user.
    """
    project_id = await applied.fetchval(
        INSERT_PROJECT_SQL,
        BOOTSTRAP_WORKSPACE_ID,
        "Launch",
        "planned",
        MEMBER_ID,
    )

    with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
        await applied.execute(
            "UPDATE projects SET lead_id = $2 WHERE id = $1",
            project_id,
            OUTSIDER_ID,
        )

    assert raised.value.constraint_name == "projects_lead_fk"


async def test_removing_a_member_who_leads_a_project_is_refused(applied):
    """ON DELETE RESTRICT, demonstrated rather than read off the catalog.

    The alternative -- SET NULL on the lead column -- would make a single
    `DELETE FROM workspace_members` quietly vacate the leadership of every
    project that person ran, reporting `DELETE 1`. The caller reassigns first.

    `RestrictViolationError` and not `ForeignKeyViolationError`, which is a
    distinction worth writing down rather than discovering: asyncpg maps
    SQLSTATE 23001 to the first and 23503 to the second, and neither is a
    subclass of the other. A RESTRICT refusal on delete therefore does NOT
    reach `except asyncpg.ForeignKeyViolationError`. Nothing in ProjectService
    deletes a membership, so no handler there needs it today -- but whichever
    service grows "remove a member" does, and catching the wrong one would
    surface a routine refusal as a masked internal error.
    """
    await applied.fetchval(
        INSERT_PROJECT_SQL,
        BOOTSTRAP_WORKSPACE_ID,
        "Launch",
        "planned",
        MEMBER_ID,
    )

    with pytest.raises(asyncpg.RestrictViolationError) as raised:
        await applied.execute(
            "DELETE FROM workspace_members WHERE workspace_id = $1 AND user_id = $2",
            BOOTSTRAP_WORKSPACE_ID,
            MEMBER_ID,
        )

    assert raised.value.constraint_name == "projects_lead_fk"


async def test_the_lead_column_is_indexed_on_the_referencing_side(applied):
    """Without this index every member removal scans `projects` in full.

    PostgreSQL indexes the referenced side of a foreign key and never the
    referencing side, so the RESTRICT check above has nothing to use unless
    this migration provides it.
    """
    columns = [
        row["column_name"]
        for row in await applied.fetch(
            INDEX_COLUMNS_SQL, PROJECTS_TABLE, "projects_workspace_lead_idx"
        )
    ]

    assert columns == ["workspace_id", "lead_id"]


# ------------------------------------------------------- the other tenancy keys


async def test_a_team_from_another_workspace_cannot_join_a_project(applied):
    """`project_teams` holds one workspace_id, and both its keys read it.

    A pair of single-column foreign keys would accept this row and nothing
    would notice, which is the whole reason the join table is keyed on three
    columns rather than two.
    """
    project_id = await applied.fetchval(
        INSERT_PROJECT_SQL, BOOTSTRAP_WORKSPACE_ID, "Launch", "planned", None
    )

    with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
        await applied.execute(
            "INSERT INTO project_teams (workspace_id, project_id, team_id) "
            "VALUES ($1, $2, $3)",
            BOOTSTRAP_WORKSPACE_ID,
            project_id,
            OTHER_TEAM_ID,
        )

    assert raised.value.constraint_name == "project_teams_team_fk"


async def test_an_issue_may_not_take_a_milestone_from_a_different_project(applied):
    """`issues_milestone_fk` carries `project_id` into the referenced key.

    A two-column key onto (workspace_id, id) would accept this happily: both
    projects are in the same tenant, and the milestone is real. Only the third
    column makes a milestone meaningful solely inside the project that owns it.
    """
    mine = await applied.fetchval(
        INSERT_PROJECT_SQL, BOOTSTRAP_WORKSPACE_ID, "Mine", "planned", None
    )
    theirs = await applied.fetchval(
        INSERT_PROJECT_SQL, BOOTSTRAP_WORKSPACE_ID, "Theirs", "planned", None
    )

    foreign_milestone = await applied.fetchval(
        """
        INSERT INTO project_milestones (workspace_id, project_id, name, position)
        VALUES ($1, $2, $3, 0)
        RETURNING id
        """,
        BOOTSTRAP_WORKSPACE_ID,
        theirs,
        "Beta",
    )

    issue_id = await _insert_issue(applied)

    with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
        await applied.execute(
            "UPDATE issues SET project_id = $2, milestone_id = $3 WHERE id = $1",
            issue_id,
            mine,
            foreign_milestone,
        )

    assert raised.value.constraint_name == "issues_milestone_fk"


async def test_an_issue_may_not_hold_a_milestone_with_no_project(applied):
    """The hole MATCH SIMPLE leaves in issues_milestone_fk, closed by a CHECK.

    With `project_id` NULL the foreign key is skipped entirely, so without
    `issues_milestone_requires_project` this row would pass untested: an issue
    in no project pointing at somebody else's milestone.
    """
    project_id = await applied.fetchval(
        INSERT_PROJECT_SQL, BOOTSTRAP_WORKSPACE_ID, "Launch", "planned", None
    )
    milestone_id = await applied.fetchval(
        """
        INSERT INTO project_milestones (workspace_id, project_id, name, position)
        VALUES ($1, $2, $3, 0)
        RETURNING id
        """,
        BOOTSTRAP_WORKSPACE_ID,
        project_id,
        "Beta",
    )

    issue_id = await _insert_issue(applied)

    with pytest.raises(asyncpg.CheckViolationError) as raised:
        await applied.execute(
            "UPDATE issues SET project_id = NULL, milestone_id = $2 WHERE id = $1",
            issue_id,
            milestone_id,
        )

    assert raised.value.constraint_name == "issues_milestone_requires_project"


# ------------------------------------------------------------------- the state


@pytest.mark.parametrize("state", PROJECT_STATES)
async def test_every_state_the_domain_names_is_one_the_column_accepts(applied, state):
    """`projects_state_check` and PROJECT_STATES are two statements of one rule.

    Asserted by writing each value rather than by parsing the constraint: the
    question is whether the server accepts what the application will send, and
    only the server answers that.
    """
    assert (
        await applied.fetchval(
            INSERT_PROJECT_SQL, BOOTSTRAP_WORKSPACE_ID, "Launch", state, None
        )
    ) is not None


@pytest.mark.parametrize("state", REJECTED_STATES)
async def test_a_state_outside_the_vocabulary_is_refused(applied, state):
    with pytest.raises(asyncpg.CheckViolationError) as raised:
        await applied.fetchval(
            INSERT_PROJECT_SQL, BOOTSTRAP_WORKSPACE_ID, "Launch", state, None
        )

    assert raised.value.constraint_name == "projects_state_check"


async def test_the_state_column_is_text_rather_than_an_enum_type(applied):
    """A CHECK on TEXT, not a PostgreSQL enum, and the reason is the runner.

    Each migration executes inside one transaction, and PostgreSQL refuses to
    *use* a label added by `ALTER TYPE ... ADD VALUE` in the transaction that
    added it -- so the migration that one day adds a sixth state could not be
    written as one file, which is the only shape a file in `migrations/` takes.
    """
    data_type = await applied.fetchval(
        """
        SELECT data_type
        FROM information_schema.columns
        WHERE table_schema = 'public'
            AND table_name = 'projects'
            AND column_name = 'state'
        """
    )

    assert data_type == "text"


async def _insert_issue(connection) -> UUID:
    """One issue on the bootstrap team, in no project.

    Written here rather than in a fixture because only two tests need one, and
    both need it after they have built the projects it will be pointed at.
    `number` and `workflow_state_id` are NOT NULL since 005; the state is the
    board 005 seeded for this team.
    """
    workflow_state_id = await connection.fetchval(
        """
        SELECT id
        FROM workflow_states
        WHERE workspace_id = $1 AND team_id = $2
        ORDER BY position, id
        LIMIT 1
        """,
        BOOTSTRAP_WORKSPACE_ID,
        BOOTSTRAP_TEAM_ID,
    )

    issue_id: UUID = await connection.fetchval(
        """
        INSERT INTO issues (
            workspace_id, team_id, number, workflow_state_id, title, priority
        )
        VALUES ($1, $2, 1, $3, 'An issue', 1)
        RETURNING id
        """,
        BOOTSTRAP_WORKSPACE_ID,
        BOOTSTRAP_TEAM_ID,
        workflow_state_id,
    )

    return issue_id
