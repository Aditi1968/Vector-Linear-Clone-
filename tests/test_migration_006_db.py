"""Migration 006 applied by the real runner, then interrogated by the server.

`test_migration_lint.py` reads 006 as text and `test_issue_fields_db.py`
exercises the application over the schema it leaves behind. Neither answers
what this file asks, because 006's central claim is one that only PostgreSQL
can confirm or refute:

  * `issues_assignee_fk` is a *composite* key over
    `(workspace_id, assignee_id)` onto `workspace_members
    (workspace_id, user_id)`, and that is the whole enforcement of "an issue
    may only be assigned to a member of its own workspace". Two single-column
    keys -- assignee to `users`, workspace to `workspaces` -- would both be
    satisfied by an issue in workspace A assigned to a real user who is a
    member only of workspace B, and no catalog summary that skips the column
    ORDER can tell the two apart. Only an INSERT can.

  * `ON DELETE SET NULL (assignee_id)` carries a column list, and the list is
    load-bearing rather than decorative. The bare form nulls *every*
    referencing column, which here includes `issues.workspace_id` -- NOT NULL
    since 002 -- so an offboarding would fail on the constraint instead of
    unassigning the leaver's work. The difference shows up only when a
    membership is actually deleted.

  * `issues_creator_fk` points at `users` and NOT at `workspace_members`, on
    purpose: "who is assigned" is a live claim that stops being true when
    someone leaves, and "who filed this" is a historical fact that does not.
    A test that only checked both columns go NULL on deletion would pass
    against a schema that had confused the two.

So this file builds the production genealogy -- every migration in order,
through `scripts.apply_migration` exactly as an operator would -- and then
puts each claim to the server as a statement that must succeed or must fail.

Marked `db`: deselected by default, skipped when Docker is unreachable.
"""

from pathlib import Path
from uuid import UUID

import asyncpg
import pytest

from scripts.apply_migration import apply_migration

from tests.conftest import reset_schema


pytestmark = pytest.mark.db

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"

# Listed rather than globbed. This file's subject is one migration, so it
# applies its own prefix: 006 needs 003's `users` and 004's
# `workspace_members` to reference at all, which is why -- unlike
# test_migration_005_db.py -- the prefix here is the whole chain. Globbing
# would silently start applying 007 underneath a file that asserts what 006
# built.
PREFIX = (
    "001_issues.sql",
    "002_tenancy.sql",
    "003_auth.sql",
    "004_membership.sql",
    "005_team_workflows.sql",
    "006_issue_fields.sql",
)

BOOTSTRAP_WORKSPACE_ID = UUID("00000000-0000-7000-8000-000000000001")
BOOTSTRAP_TEAM_ID = UUID("00000000-0000-7000-8000-000000000002")

# A second tenant, which is what makes "a member of another workspace"
# expressible at all. Without it every assignee is trivially valid and the
# composite key proves nothing.
OTHER_WORKSPACE_ID = UUID("00000000-0000-7000-8000-0000000000b1")

MEMBER_ID = UUID("00000000-0000-7000-8000-0000000000c1")
OUTSIDER_ID = UUID("00000000-0000-7000-8000-0000000000c2")

ISSUE_ID = UUID("00000000-0000-7000-8000-0000000000d1")

LIVE_INDEX = "issues_workspace_live_created_at_id_idx"

# Enough to satisfy `users_password_hash_argon2id`, which only constrains the
# prefix. Nothing here verifies a password, so a real hash would be cost
# without a question, and the same literal is what
# test_migration_004_db.py uses.
PASSWORD_HASH = "$argon2id$not-a-real-hash"

# The five columns 006 adds, fingerprinted the way the sibling migration
# suites fingerprint theirs: column -> (type as information_schema spells it,
# nullable, normalized default).
#
# Every one is nullable and every default is None, and both halves are the
# assertion. These columns describe an issue that has no assignee, no
# estimate, no due date and is not archived -- which is the ordinary state of
# a new issue, not a half-migrated one -- so a NOT NULL here would demand a
# value the product has none to give. A DEFAULT on `archived_at` would be
# worse than wrong: every issue would be born off the board.
COLUMNS_ADDED = {
    "assignee_id": ("uuid", True, None),
    "creator_id": ("uuid", True, None),
    "estimate": ("integer", True, None),
    "due_date": ("date", True, None),
    "archived_at": ("timestamp with time zone", True, None),
}

COLUMNS_SQL = """
SELECT column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_schema = 'public' AND table_name = 'issues'
"""

# Constraints by structure. `conkey`/`confkey` are unnested WITH ORDINALITY
# and resolved to column names in order, because the order is most of the
# assertion.
CONSTRAINT_SQL = """
SELECT
    con.confdeltype::text AS delete_action,
    con.confupdtype::text AS update_action,
    referenced.relname::text AS referenced_table,
    pg_get_constraintdef(con.oid) AS definition,
    (
        SELECT array_agg(att.attname::text ORDER BY local_key.ord)
        FROM unnest(con.conkey) WITH ORDINALITY AS local_key(attnum, ord)
        JOIN pg_attribute att
            ON att.attrelid = con.conrelid AND att.attnum = local_key.attnum
    ) AS local_columns,
    (
        SELECT array_agg(att.attname::text ORDER BY foreign_key.ord)
        FROM unnest(con.confkey) WITH ORDINALITY AS foreign_key(attnum, ord)
        JOIN pg_attribute att
            ON att.attrelid = con.confrelid AND att.attnum = foreign_key.attnum
    ) AS referenced_columns
FROM pg_constraint con
LEFT JOIN pg_class referenced ON referenced.oid = con.confrelid
WHERE con.conrelid = 'issues'::regclass AND con.conname = $1
"""

INSERT_ISSUE = """
    INSERT INTO issues (
        id, workspace_id, team_id, number, workflow_state_id,
        title, priority, assignee_id, creator_id, estimate
    )
    VALUES ($1, $2, $3, $4, $5, 'Ship it', 0, $6, $7, $8)
"""

SET_NULL = "n"
RESTRICT = "r"


async def _default_state(connection, workspace_id: UUID, team_id: UUID) -> UUID:
    return await connection.fetchval(
        """
        SELECT id FROM workflow_states
        WHERE workspace_id = $1 AND team_id = $2 AND type = 'unstarted'
        """,
        workspace_id,
        team_id,
    )


@pytest.fixture
async def migrated(postgres_dsn):
    """The whole chain applied through the runner, plus two tenants of people.

    The container is session-scoped, so the schema is dropped first rather
    than assumed empty.

    `MEMBER_ID` is a member of the bootstrap workspace and `OUTSIDER_ID` of
    the other one. Both are real rows in `users` -- that is the point. An
    assignee who does not exist at all would be refused by any shape of key,
    including the wrong one, so the outsider has to be a genuine user who is
    genuinely a member of somewhere else.
    """
    connection = await asyncpg.connect(postgres_dsn)

    try:
        await reset_schema(connection)

        for name in PREFIX:
            async with connection.transaction():
                await apply_migration(
                    connection,
                    MIGRATIONS_DIR / name,
                    migrations_dir=MIGRATIONS_DIR,
                )

        await connection.execute(
            "INSERT INTO workspaces (id, slug, name) VALUES ($1, 'other', 'Other')",
            OTHER_WORKSPACE_ID,
        )
        await connection.executemany(
            "INSERT INTO users (id, email, password_hash) VALUES ($1, $2, $3)",
            [
                (MEMBER_ID, "member@vector.test", PASSWORD_HASH),
                (OUTSIDER_ID, "outsider@vector.test", PASSWORD_HASH),
            ],
        )
        await connection.executemany(
            """
            INSERT INTO workspace_members (workspace_id, user_id, role)
            VALUES ($1, $2, 'member')
            """,
            [
                (BOOTSTRAP_WORKSPACE_ID, MEMBER_ID),
                (OTHER_WORKSPACE_ID, OUTSIDER_ID),
            ],
        )

        yield connection
    finally:
        await connection.close()


async def _insert_issue(
    connection,
    *,
    assignee_id: UUID | None = None,
    creator_id: UUID | None = None,
    estimate: int | None = None,
    number: int = 1,
    issue_id: UUID = ISSUE_ID,
) -> None:
    state_id = await _default_state(
        connection, BOOTSTRAP_WORKSPACE_ID, BOOTSTRAP_TEAM_ID
    )

    await connection.execute(
        INSERT_ISSUE,
        issue_id,
        BOOTSTRAP_WORKSPACE_ID,
        BOOTSTRAP_TEAM_ID,
        number,
        state_id,
        assignee_id,
        creator_id,
        estimate,
    )


async def test_the_five_columns_are_nullable_and_carry_no_default(migrated):
    rows = await migrated.fetch(COLUMNS_SQL)
    actual = {
        row["column_name"]: (
            row["data_type"],
            row["is_nullable"] == "YES",
            row["column_default"],
        )
        for row in rows
    }

    for name, expected in COLUMNS_ADDED.items():
        assert actual[name] == expected, name


async def test_estimate_refuses_a_negative_but_allows_zero_and_nothing(migrated):
    """Zero is a team saying the work is free; NULL is nobody having sized it.

    Both are meanings the product has, so the bound is 0 rather than 1 and the
    column stays nullable. Only a negative estimate has no reading to give it.
    """
    await _insert_issue(migrated, estimate=0, number=1, issue_id=UUID(int=1))
    await _insert_issue(migrated, estimate=None, number=2, issue_id=UUID(int=2))

    with pytest.raises(asyncpg.CheckViolationError) as exc_info:
        await _insert_issue(migrated, estimate=-1, number=3, issue_id=UUID(int=3))

    assert exc_info.value.constraint_name == "issues_estimate_non_negative"


async def test_the_assignee_key_is_composite_over_the_membership_pair(migrated):
    """Two single-column keys would look the same in every summary but one.

    The column ORDER is most of the assertion: `(workspace_id, assignee_id)`
    referencing `(workspace_id, user_id)` is the tuple check, and any other
    arrangement of the same four names is a different rule.
    """
    row = await migrated.fetchrow(CONSTRAINT_SQL, "issues_assignee_fk")

    assert row is not None, "issues_assignee_fk does not exist"
    assert row["referenced_table"] == "workspace_members"
    assert row["local_columns"] == ["workspace_id", "assignee_id"]
    assert row["referenced_columns"] == ["workspace_id", "user_id"]
    assert row["delete_action"] == SET_NULL
    assert row["update_action"] == RESTRICT

    # The column list on SET NULL. Without it PostgreSQL nulls every
    # referencing column, `issues.workspace_id` included -- which is NOT NULL,
    # so the deletion this clause exists to permit would fail instead.
    assert "ON DELETE SET NULL (assignee_id)" in row["definition"]


async def test_an_issue_cannot_be_assigned_to_a_member_of_another_workspace(migrated):
    """The tenant rule, enforced by the server in the same statement.

    OUTSIDER_ID is a real user and a real member -- of somewhere else. A
    single-column key onto `users` would accept this row, and the assignment
    would be a cross-tenant one that no application check happened to catch.
    """
    with pytest.raises(asyncpg.ForeignKeyViolationError) as exc_info:
        await _insert_issue(migrated, assignee_id=OUTSIDER_ID)

    assert exc_info.value.constraint_name == "issues_assignee_fk"

    # And the same id, in the workspace they *are* a member of, is fine --
    # otherwise the test above would also pass against a key that refused
    # every assignee.
    assert await migrated.fetchval("SELECT count(*) FROM issues") == 0

    await _insert_issue(migrated, assignee_id=MEMBER_ID)

    assert await migrated.fetchval("SELECT assignee_id FROM issues") == MEMBER_ID


async def test_removing_a_member_unassigns_their_work_and_keeps_the_workspace(
    migrated,
):
    """Offboarding is an ordinary operation and must not be blocked by it.

    The second assertion is the one the column list on SET NULL exists for: a
    bare `ON DELETE SET NULL` would try to null `workspace_id` too, and this
    DELETE would fail on the NOT NULL rather than unassigning anything.
    """
    await _insert_issue(migrated, assignee_id=MEMBER_ID)

    await migrated.execute(
        "DELETE FROM workspace_members WHERE workspace_id = $1 AND user_id = $2",
        BOOTSTRAP_WORKSPACE_ID,
        MEMBER_ID,
    )

    row = await migrated.fetchrow(
        "SELECT assignee_id, workspace_id FROM issues WHERE id = $1", ISSUE_ID
    )

    assert row["assignee_id"] is None
    assert row["workspace_id"] == BOOTSTRAP_WORKSPACE_ID


async def test_the_creator_survives_the_author_leaving_the_workspace(migrated):
    """Authorship is a historical fact, not a live claim about membership.

    This is the assertion that separates `issues_creator_fk` from
    `issues_assignee_fk`. Both columns hold a user id and both go NULL when a
    user is deleted, so a suite that only checked deletion would pass against
    a schema that had pointed the creator at `workspace_members` too -- and
    that schema would erase the authorship of every issue a leaver ever filed.
    """
    await _insert_issue(migrated, assignee_id=MEMBER_ID, creator_id=MEMBER_ID)

    await migrated.execute(
        "DELETE FROM workspace_members WHERE workspace_id = $1 AND user_id = $2",
        BOOTSTRAP_WORKSPACE_ID,
        MEMBER_ID,
    )

    row = await migrated.fetchrow(
        "SELECT assignee_id, creator_id FROM issues WHERE id = $1", ISSUE_ID
    )

    assert row["assignee_id"] is None, "leaving the workspace must unassign"
    assert row["creator_id"] == MEMBER_ID, "leaving the workspace must not un-author"


async def test_deleting_the_account_clears_the_creator_rather_than_blocking(migrated):
    """RESTRICT here would make an account that ever filed an issue undeletable.

    NULL is a state this column already defines -- every issue predating 003
    is in it -- so the read path needs no new case.
    """
    await _insert_issue(migrated, creator_id=MEMBER_ID)

    await migrated.execute(
        "DELETE FROM workspace_members WHERE workspace_id = $1 AND user_id = $2",
        BOOTSTRAP_WORKSPACE_ID,
        MEMBER_ID,
    )
    await migrated.execute("DELETE FROM users WHERE id = $1", MEMBER_ID)

    assert await migrated.fetchval("SELECT creator_id FROM issues") is None


async def test_the_live_issue_index_is_partial_on_archived_at(migrated):
    """The predicate is the point, not the columns.

    Without `WHERE archived_at IS NULL` the default list walk reads archived
    entries and discards them, so serving fifty live issues costs in
    proportion to every issue the workspace has ever created. In a tracker
    archived is where issues end up, so that is most of the table.
    """
    definition = await migrated.fetchval(
        "SELECT indexdef FROM pg_indexes WHERE indexname = $1", LIVE_INDEX
    )

    assert definition is not None, f"{LIVE_INDEX} does not exist"
    assert "WHERE (archived_at IS NULL)" in definition
    assert "created_at DESC" in definition
    assert "id DESC" in definition

    # 002's unpartitioned index is deliberately kept: it is what the archive
    # view reads, and this partial index cannot serve that query at all.
    assert await migrated.fetchval(
        "SELECT count(*) FROM pg_indexes WHERE indexname = $1",
        "issues_workspace_created_at_id_idx",
    )
