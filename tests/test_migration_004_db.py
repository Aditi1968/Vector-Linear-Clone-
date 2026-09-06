"""Migration 004 applied by the real runner, then asked what it built.

`test_migration_lint.py` reads 004 as text and `test_membership_scope.py`
drives a service against the schema it produces. Neither can answer the
questions this migration actually raises, because the interesting parts are
all invisible in a catalog summary and all one word apart in the file:

  * a primary key over (workspace_id, user_id) and a surrogate key with a
    UNIQUE beside it read the same in every listing that does not print the
    key's columns, and differ the day something declares a foreign key
    against the pair;
  * `ON DELETE RESTRICT` and `ON DELETE CASCADE` differ by a refused delete
    versus the silent removal of every grant into a workspace;
  * a CHECK constraint that lists two of three roles rejects nothing anyone
    tries by hand and rejects a real membership in production.

So this file builds the schema through `scripts.apply_migration` exactly as
an operator would and then asks the server. Where 003 is present it is
applied; where it is not, the narrowest possible `users` stands in -- see
tests/test_membership_scope.py, which carries the same stand-in and the
reasoning for it.

Two disciplines are borrowed from tests/test_migration_002_db.py: action
bytes are pinned as bytes rather than read out of `pg_get_constraintdef`,
because the byte is what the executor consults; and expected values are
written as literals rather than fetched from the database being checked.

Marked `db`: deselected by default, skipped when Docker is unreachable.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import asyncpg
import pytest

from scripts.apply_migration import apply_migration, read_migration


pytestmark = pytest.mark.db

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"
MIGRATION_001 = MIGRATIONS_DIR / "001_issues.sql"
MIGRATION_002 = MIGRATIONS_DIR / "002_tenancy.sql"
MIGRATION_003 = MIGRATIONS_DIR / "003_auth.sql"
MIGRATION_004 = MIGRATIONS_DIR / "004_membership.sql"

BOOTSTRAP_WORKSPACE_ID = UUID("00000000-0000-7000-8000-000000000001")
OTHER_WORKSPACE_ID = UUID("00000000-0000-7000-8000-0000000000a1")

USER_ID = UUID("00000000-0000-7000-8000-0000000000e1")
OTHER_USER_ID = UUID("00000000-0000-7000-8000-0000000000e2")
UNKNOWN_USER_ID = UUID("00000000-0000-7000-8000-0000000000ff")

MEMBERS_TABLE = "public.workspace_members"
INVITATIONS_TABLE = "public.workspace_invitations"

MEMBERS_PRIMARY_KEY = ("workspace_id", "user_id")
MEMBERS_USER_INDEX = "workspace_members_user_idx"
INVITATIONS_WORKSPACE_INDEX = "workspace_invitations_workspace_idx"

ROLES = ("member", "admin", "owner")

# Roles the CHECK must reject. Capitalisations are in the list on purpose:
# `IN` is case-sensitive, so 'Owner' is a different string and a constraint
# that admitted it would let one role be spelled two ways.
REJECTED_ROLES = ("", "guest", "superuser", "Owner", "OWNER", "owner ", " owner")

# The single-byte codes pg_constraint stores for referential actions, pinned
# as bytes for the reason given in tests/test_migration_002_db.py: the byte is
# what the executor consults, and pinning it is what stops RESTRICT drifting
# into CASCADE behind a rendering that still reads plausibly.
NO_ACTION = "a"
RESTRICT = "r"
CASCADE = "c"
SET_NULL = "n"
SET_DEFAULT = "d"

NOT_RESTRICT = frozenset({NO_ACTION, CASCADE, SET_NULL, SET_DEFAULT})

# A sha256-shaped digest, and six strings that are not one: empty, plainly
# not hex, uppercase hex, one character short, one character long, and an
# email address written into the column by a caller that confused the two.
TOKEN_HASH = "a" * 64
REJECTED_TOKEN_HASHES = (
    "",
    "not-a-digest",
    "A" * 64,
    "a" * 63,
    "a" * 65,
    "invitee@example.com",
)

STANDIN_USERS_SQL = """
CREATE TABLE users (
    id UUID PRIMARY KEY
)
"""

# Dropped after every test as well as before it; see the identically named
# constant in tests/test_membership_scope.py for why the teardown half is the
# one that matters.
DROP_MEMBERSHIP_TABLES_SQL = (
    "DROP TABLE IF EXISTS workspace_invitations, workspace_members"
)

INSERT_WORKSPACE_SQL = """
INSERT INTO workspaces (id, slug, name)
VALUES ($1, $2, $3)
"""

INSERT_USER_SQL = "INSERT INTO users (id) VALUES ($1)"

INSERT_MEMBER_SQL = """
INSERT INTO workspace_members (workspace_id, user_id, role)
VALUES ($1, $2, $3)
"""

INSERT_INVITATION_SQL = """
INSERT INTO workspace_invitations (
    workspace_id, email, role, token_hash, expires_at
)
VALUES ($1, $2, $3, $4, $5)
"""

COLUMNS_SQL = """
SELECT column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_schema = 'public' AND table_name = $1
"""

PRIMARY_KEY_SQL = """
SELECT pg_attribute.attname
FROM pg_constraint
CROSS JOIN LATERAL
    unnest(pg_constraint.conkey) WITH ORDINALITY AS key_column(attnum, ord)
JOIN pg_attribute
    ON pg_attribute.attrelid = pg_constraint.conrelid
    AND pg_attribute.attnum = key_column.attnum
WHERE pg_constraint.conrelid = $1::regclass
    AND pg_constraint.contype = 'p'
ORDER BY key_column.ord
"""

# Referential actions and the referenced table, per foreign key.
#
# The three action columns are cast to text because they are `"char"`, which
# asyncpg decodes to a one-byte `bytes` rather than a `str` -- so an
# uncast comparison against 'r' fails against a schema that is correct.
# `confrelid::regclass` names what the key points at, which is what catches a
# constraint that restricts correctly against the wrong table.
FOREIGN_KEYS_SQL = """
SELECT
    conname,
    confrelid::regclass::text AS referenced_table,
    confupdtype::text AS update_action,
    confdeltype::text AS delete_action
FROM pg_constraint
WHERE conrelid = $1::regclass AND contype = 'f'
"""

# `contype = 'c'` matters on PostgreSQL 18, where NOT NULL constraints are
# catalogued too (as `contype = 'n'`).
CHECK_CONSTRAINTS_SQL = """
SELECT conname
FROM pg_constraint
WHERE conrelid = $1::regclass AND contype = 'c'
"""

INDEX_COLUMNS_SQL = """
SELECT
    pg_attribute.attname AS column_name,
    pg_index.indisunique AS is_unique
FROM pg_index
JOIN pg_class ON pg_class.oid = pg_index.indexrelid
CROSS JOIN LATERAL
    unnest(pg_index.indkey::smallint[]) WITH ORDINALITY AS key_column(attnum, ord)
LEFT JOIN pg_attribute
    ON pg_attribute.attrelid = pg_index.indrelid
    AND pg_attribute.attnum = key_column.attnum
WHERE pg_index.indrelid = $1::regclass
    AND pg_class.relname = $2
ORDER BY key_column.ord
"""


def expires_at() -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=7)


@pytest.fixture
async def applied(postgres_dsn):
    """001, 002, `users`, 004 -- then a workspace and two accounts.

    Every table is dropped first because the container is shared for the
    whole session, so a `workspaces` left behind by another file would make
    002's CREATE TABLE fail here for reasons unrelated to this migration.
    """
    connection = await asyncpg.connect(postgres_dsn)

    try:
        await connection.execute(
            "DROP TABLE IF EXISTS "
            "workspace_invitations, workspace_members, issues, teams, "
            "workspaces, users"
        )
        await connection.execute("DROP TABLE IF EXISTS schema_migrations")
        await connection.execute(read_migration(MIGRATION_001))

        async with connection.transaction():
            await apply_migration(
                connection, MIGRATION_002, migrations_dir=MIGRATIONS_DIR
            )

        if MIGRATION_003.is_file():
            async with connection.transaction():
                await apply_migration(
                    connection, MIGRATION_003, migrations_dir=MIGRATIONS_DIR
                )
        else:
            await connection.execute(STANDIN_USERS_SQL)

        async with connection.transaction():
            await apply_migration(
                connection, MIGRATION_004, migrations_dir=MIGRATIONS_DIR
            )

        await connection.execute(
            INSERT_WORKSPACE_SQL, OTHER_WORKSPACE_ID, "acme", "Acme"
        )
        await connection.execute(INSERT_USER_SQL, USER_ID)
        await connection.execute(INSERT_USER_SQL, OTHER_USER_ID)

        yield connection
    finally:
        try:
            await connection.execute(DROP_MEMBERSHIP_TABLES_SQL)
        finally:
            await connection.close()


async def _foreign_keys(connection, table: str) -> dict[str, dict]:
    rows = await connection.fetch(FOREIGN_KEYS_SQL, table)

    return {row["conname"]: dict(row) for row in rows}


# --- workspace_members ------------------------------------------------


async def test_the_membership_table_has_exactly_four_columns(applied):
    """Four, and no surrogate id among them.

    An `id` column would not break anything today, which is why it is worth
    an assertion: it would arrive as a harmless addition and leave the pair
    as merely unique rather than as the row's identity.
    """
    rows = await applied.fetch(COLUMNS_SQL, "workspace_members")
    columns = {row["column_name"]: row for row in rows}

    assert set(columns) == {"workspace_id", "user_id", "role", "created_at"}

    assert columns["workspace_id"]["data_type"] == "uuid"
    assert columns["user_id"]["data_type"] == "uuid"
    assert columns["role"]["data_type"] == "text"
    assert columns["created_at"]["data_type"] == "timestamp with time zone"

    for column in columns.values():
        assert column["is_nullable"] == "NO"


async def test_the_role_column_has_no_default(applied):
    """A default would make a forgotten role into a silent grant."""
    rows = await applied.fetch(COLUMNS_SQL, "workspace_members")
    role = next(row for row in rows if row["column_name"] == "role")

    assert role["column_default"] is None


async def test_the_primary_key_is_the_workspace_and_user_pair_in_that_order(applied):
    """Column order is part of the key, and part of what it can serve.

    A key on (user_id, workspace_id) is a different index and a different
    FK target; the pair is only usable as the target of `issues
    (workspace_id, assignee_id)` in this order.
    """
    rows = await applied.fetch(PRIMARY_KEY_SQL, MEMBERS_TABLE)

    assert tuple(row["attname"] for row in rows) == MEMBERS_PRIMARY_KEY


async def test_a_user_cannot_join_the_same_workspace_twice(applied):
    """The primary key, observed rather than read out of the catalog.

    Two rows for one pair is two answers to "what role does this user
    hold here", and whichever the planner returns first becomes the
    permission the request runs with.
    """
    await applied.execute(INSERT_MEMBER_SQL, OTHER_WORKSPACE_ID, USER_ID, "member")

    with pytest.raises(asyncpg.UniqueViolationError):
        await applied.execute(INSERT_MEMBER_SQL, OTHER_WORKSPACE_ID, USER_ID, "owner")


async def test_the_same_user_may_join_two_workspaces(applied):
    """The other half of the key: the pair is unique, neither column is."""
    await applied.execute(INSERT_MEMBER_SQL, OTHER_WORKSPACE_ID, USER_ID, "member")
    await applied.execute(INSERT_MEMBER_SQL, BOOTSTRAP_WORKSPACE_ID, USER_ID, "owner")

    count = await applied.fetchval(
        "SELECT count(*) FROM workspace_members WHERE user_id = $1",
        USER_ID,
    )

    assert count == 2


@pytest.mark.parametrize("role", ROLES)
async def test_every_role_in_the_vocabulary_is_accepted(applied, role):
    await applied.execute(INSERT_MEMBER_SQL, OTHER_WORKSPACE_ID, USER_ID, role)

    stored = await applied.fetchval(
        "SELECT role FROM workspace_members WHERE user_id = $1",
        USER_ID,
    )

    assert stored == role


@pytest.mark.parametrize("role", REJECTED_ROLES)
async def test_a_role_outside_the_vocabulary_is_rejected(applied, role):
    with pytest.raises(asyncpg.CheckViolationError):
        await applied.execute(INSERT_MEMBER_SQL, OTHER_WORKSPACE_ID, USER_ID, role)


async def test_a_membership_cannot_name_a_workspace_that_does_not_exist(applied):
    absent_workspace = UUID("00000000-0000-7000-8000-0000000000cc")

    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await applied.execute(INSERT_MEMBER_SQL, absent_workspace, USER_ID, "member")


async def test_a_membership_cannot_name_a_user_that_does_not_exist(applied):
    """The constraint that makes 004 depend on 003.

    Without it a membership row could grant access on behalf of an account
    nobody created, which is a grant nobody can revoke by deleting a user.
    """
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await applied.execute(
            INSERT_MEMBER_SQL, OTHER_WORKSPACE_ID, UNKNOWN_USER_ID, "member"
        )


async def test_both_membership_foreign_keys_restrict_on_delete_and_update(applied):
    keys = await _foreign_keys(applied, MEMBERS_TABLE)

    assert set(keys) == {
        "workspace_members_workspace_fk",
        "workspace_members_user_fk",
    }

    assert keys["workspace_members_workspace_fk"]["referenced_table"] == "workspaces"
    assert keys["workspace_members_user_fk"]["referenced_table"] == "users"

    for name, key in keys.items():
        assert key["delete_action"] == RESTRICT, (
            f"{name} deletes with {key['delete_action']!r}, not RESTRICT"
        )
        assert key["delete_action"] not in NOT_RESTRICT
        assert key["update_action"] == RESTRICT


async def test_deleting_a_workspace_with_members_is_refused(applied):
    """RESTRICT, observed rather than read out of the catalog.

    `RestrictViolationError` rather than `ForeignKeyViolationError`, and the
    difference is not a detail of asyncpg's class names: PostgreSQL raises
    SQLSTATE 23001 for an explicit ON DELETE RESTRICT and 23503 for
    NO ACTION, and the two asyncpg classes are siblings with neither a
    subclass of the other. So this assertion distinguishes the constraint
    004 declares from the deferrable one it would have had by default --
    the same reasoning as tests/test_migration_002_db.py.

    Under CASCADE the delete would instead remove every grant into the
    workspace and report success.
    """
    await applied.execute(INSERT_MEMBER_SQL, OTHER_WORKSPACE_ID, USER_ID, "owner")

    with pytest.raises(asyncpg.RestrictViolationError):
        await applied.execute(
            "DELETE FROM workspaces WHERE id = $1",
            OTHER_WORKSPACE_ID,
        )

    survived = await applied.fetchval(
        "SELECT count(*) FROM workspace_members WHERE workspace_id = $1",
        OTHER_WORKSPACE_ID,
    )

    assert survived == 1


async def test_deleting_a_user_with_memberships_is_refused(applied):
    """The users side of the same guarantee; see the note above on 23001."""
    await applied.execute(INSERT_MEMBER_SQL, OTHER_WORKSPACE_ID, USER_ID, "owner")

    with pytest.raises(asyncpg.RestrictViolationError):
        await applied.execute("DELETE FROM users WHERE id = $1", USER_ID)

    survived = await applied.fetchval(
        "SELECT count(*) FROM workspace_members WHERE user_id = $1",
        USER_ID,
    )

    assert survived == 1


async def test_the_user_index_exists_and_leads_with_user_id(applied):
    """Leading column, because that is what makes it usable at all.

    Without an index leading on user_id, both the users-side RESTRICT check
    and "which workspaces am I in" scan the whole table.
    """
    rows = await applied.fetch(INDEX_COLUMNS_SQL, MEMBERS_TABLE, MEMBERS_USER_INDEX)

    assert rows, f"{MEMBERS_USER_INDEX} is missing"
    assert rows[0]["column_name"] == "user_id"
    assert not rows[0]["is_unique"]


# --- workspace_invitations --------------------------------------------


async def test_an_invitation_needs_a_workspace_that_exists(applied):
    absent_workspace = UUID("00000000-0000-7000-8000-0000000000cc")

    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await applied.execute(
            INSERT_INVITATION_SQL,
            absent_workspace,
            "invitee@example.com",
            "member",
            TOKEN_HASH,
            expires_at(),
        )


async def test_an_invitation_stores_what_it_was_given(applied):
    await applied.execute(
        INSERT_INVITATION_SQL,
        OTHER_WORKSPACE_ID,
        "Invitee@Example.com",
        "admin",
        TOKEN_HASH,
        expires_at(),
    )

    row = await applied.fetchrow(
        """
        SELECT workspace_id, email, role, token_hash, accepted_at
        FROM workspace_invitations
        """
    )

    # The email is stored as written: it is descriptive here, not a key.
    assert row["email"] == "Invitee@Example.com"
    assert row["role"] == "admin"
    assert row["token_hash"] == TOKEN_HASH
    assert row["accepted_at"] is None


@pytest.mark.parametrize("role", ROLES)
async def test_an_invitation_may_offer_any_role_a_membership_may_hold(applied, role):
    """Both CHECKs admit the same set, so no invitation is unacceptable."""
    await applied.execute(
        INSERT_INVITATION_SQL,
        OTHER_WORKSPACE_ID,
        f"{role}@example.com",
        role,
        TOKEN_HASH[:-1] + str(ROLES.index(role)),
        expires_at(),
    )


async def test_an_invitation_role_outside_the_vocabulary_is_rejected(applied):
    with pytest.raises(asyncpg.CheckViolationError):
        await applied.execute(
            INSERT_INVITATION_SQL,
            OTHER_WORKSPACE_ID,
            "invitee@example.com",
            "guest",
            TOKEN_HASH,
            expires_at(),
        )


@pytest.mark.parametrize("token_hash", REJECTED_TOKEN_HASHES)
async def test_a_token_hash_that_is_not_a_lowercase_sha256_is_rejected(
    applied, token_hash
):
    with pytest.raises(asyncpg.CheckViolationError):
        await applied.execute(
            INSERT_INVITATION_SQL,
            OTHER_WORKSPACE_ID,
            "invitee@example.com",
            "member",
            token_hash,
            expires_at(),
        )


async def test_two_invitations_cannot_share_a_token_hash(applied):
    """Redemption is by hash, so a shared one is an ambiguous credential."""
    await applied.execute(
        INSERT_INVITATION_SQL,
        OTHER_WORKSPACE_ID,
        "first@example.com",
        "member",
        TOKEN_HASH,
        expires_at(),
    )

    with pytest.raises(asyncpg.UniqueViolationError):
        await applied.execute(
            INSERT_INVITATION_SQL,
            BOOTSTRAP_WORKSPACE_ID,
            "second@example.com",
            "member",
            TOKEN_HASH,
            expires_at(),
        )


async def test_an_invitation_that_expires_before_it_is_created_is_rejected(applied):
    with pytest.raises(asyncpg.CheckViolationError):
        await applied.execute(
            INSERT_INVITATION_SQL,
            OTHER_WORKSPACE_ID,
            "invitee@example.com",
            "member",
            TOKEN_HASH,
            datetime.now(timezone.utc) - timedelta(days=1),
        )


async def test_an_invitation_requires_an_expiry(applied):
    """NOT NULL, because an invitation without one never stops working."""
    with pytest.raises(asyncpg.NotNullViolationError):
        await applied.execute(
            INSERT_INVITATION_SQL,
            OTHER_WORKSPACE_ID,
            "invitee@example.com",
            "member",
            TOKEN_HASH,
            None,
        )


async def test_two_invitations_may_go_to_the_same_address(applied):
    """Deliberately allowed: an expired invitation and its replacement."""
    for suffix in ("0", "1"):
        await applied.execute(
            INSERT_INVITATION_SQL,
            OTHER_WORKSPACE_ID,
            "invitee@example.com",
            "member",
            TOKEN_HASH[:-1] + suffix,
            expires_at(),
        )

    count = await applied.fetchval("SELECT count(*) FROM workspace_invitations")

    assert count == 2


async def test_the_invitation_workspace_foreign_key_restricts(applied):
    keys = await _foreign_keys(applied, INVITATIONS_TABLE)

    assert set(keys) == {"workspace_invitations_workspace_fk"}

    key = keys["workspace_invitations_workspace_fk"]

    assert key["referenced_table"] == "workspaces"
    assert key["delete_action"] == RESTRICT
    assert key["update_action"] == RESTRICT


async def test_the_invitation_workspace_index_exists(applied):
    rows = await applied.fetch(
        INDEX_COLUMNS_SQL, INVITATIONS_TABLE, INVITATIONS_WORKSPACE_INDEX
    )

    assert rows, f"{INVITATIONS_WORKSPACE_INDEX} is missing"
    assert rows[0]["column_name"] == "workspace_id"


async def test_there_is_no_foreign_key_from_an_invitation_to_a_user(applied):
    """An invitee has no account yet, so there is nothing to reference.

    Stated as a test because adding one looks like tightening the schema
    and would instead make it impossible to invite anyone who has not
    already signed up.
    """
    keys = await _foreign_keys(applied, INVITATIONS_TABLE)

    assert all(key["referenced_table"] != "users" for key in keys.values())


# --- the migration as a whole -----------------------------------------


async def test_004_is_recorded_in_the_ledger(applied):
    versions = await applied.fetch("SELECT version FROM schema_migrations")

    assert "004" in {row["version"] for row in versions}


async def test_both_tables_carry_the_constraints_004_names(applied):
    """Named constraints, because a name is what a later migration alters.

    `DROP CONSTRAINT workspace_members_role_check` is how the role
    vocabulary is ever widened; a constraint PostgreSQL auto-named cannot
    be written down in advance.
    """
    member_checks = {
        row["conname"]
        for row in await applied.fetch(CHECK_CONSTRAINTS_SQL, MEMBERS_TABLE)
    }
    invitation_checks = {
        row["conname"]
        for row in await applied.fetch(CHECK_CONSTRAINTS_SQL, INVITATIONS_TABLE)
    }

    assert member_checks == {"workspace_members_role_check"}
    assert invitation_checks == {
        "workspace_invitations_role_check",
        "workspace_invitations_token_hash_format",
        "workspace_invitations_expiry_after_creation",
    }


async def test_004_seeds_no_memberships_and_no_invitations(applied):
    """A migration must not hand anyone access to anything.

    The fixture inserts two users and a workspace and no membership, so a
    non-zero count here means the migration created a grant.
    """
    assert await applied.fetchval("SELECT count(*) FROM workspace_members") == 0
    assert await applied.fetchval("SELECT count(*) FROM workspace_invitations") == 0
