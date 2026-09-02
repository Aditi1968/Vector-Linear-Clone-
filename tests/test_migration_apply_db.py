"""Applies the real migration to a real PostgreSQL 18 container.

Every other test in this suite talks to a fake connection, so nothing else
would notice if 001 stopped being valid SQL on the version we deploy to.
Marked `db`: deselected by default, skipped when Docker is unreachable.

The adoption tests at the bottom are here for a sharper reason. The runner
decides that 001 is already applied by fingerprinting the live table against
`information_schema` and the `pg_catalog`, and everywhere else that
fingerprint is compared against a fake we wrote ourselves -- which would
agree with us just as readily if we had the catalog's spelling, its DESC
encoding, or its rewriting of BETWEEN wrong. Only a real server settles it.
"""

from pathlib import Path

import asyncpg
import pytest

from scripts.apply_migration import (
    CHECKSUM_OK,
    MigrationError,
    compute_checksum,
    migration_status,
)


pytestmark = pytest.mark.db

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"
MIGRATION = MIGRATIONS_DIR / "001_issues.sql"

EXPECTED_COLUMNS = {
    "id",
    "title",
    "description",
    "priority",
    "completed_at",
    "created_at",
    "updated_at",
}


@pytest.fixture
async def migrated(postgres_dsn):
    """A connection to a database holding exactly what 001 creates.

    The container is session-scoped, so the schema is dropped first rather
    than assuming this test is the only one to touch it. The ledger goes too:
    a database that 001 has reached but the runner never has is precisely the
    starting state adoption exists for.
    """
    connection = await asyncpg.connect(postgres_dsn)

    try:
        await connection.execute("DROP TABLE IF EXISTS issues")
        await connection.execute("DROP TABLE IF EXISTS schema_migrations")
        await connection.execute(MIGRATION.read_text(encoding="utf-8"))

        yield connection
    finally:
        await connection.close()


async def test_issues_table_has_exactly_the_entity_columns(migrated):
    columns = await migrated.fetch(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = 'issues'
        """
    )

    assert {row["column_name"] for row in columns} == EXPECTED_COLUMNS


async def test_keyset_index_exists(migrated):
    """IssueRepository.list orders by (created_at DESC, id DESC) with no OFFSET.

    Without this index that plan is a sort over the whole table.
    """
    index = await migrated.fetchval(
        """
        SELECT indexname
        FROM pg_indexes
        WHERE tablename = 'issues' AND indexname = 'issues_created_at_id_idx'
        """
    )

    assert index == "issues_created_at_id_idx"


async def test_the_runner_adopts_the_schema_001_actually_creates(migrated):
    """The fingerprint against a live server rather than against our fakes.

    This is the test that would fail if any expectation the runner holds --
    a data type's spelling, a default's rendering, the DESC bit in
    `indoption`, the rewritten BETWEEN -- did not match what PostgreSQL
    really produces from `migrations/001_issues.sql`.
    """
    async with migrated.transaction():
        report = await migration_status(migrated, migrations_dir=MIGRATIONS_DIR)

    ledger = await migrated.fetch(
        "SELECT version, checksum FROM schema_migrations"
    )

    assert [(row["version"], row["checksum"]) for row in ledger] == [
        ("001", compute_checksum(MIGRATION.read_text(encoding="utf-8")))
    ]

    assert [item.version for item in report.applied] == ["001"]
    assert report.applied[0].state == CHECKSUM_OK
    assert MIGRATION not in report.pending


async def test_the_runner_refuses_a_table_that_drifted_from_001(migrated):
    """Real DDL, real catalogs: refusal is not an artefact of the fakes.

    An `issues` carrying a later migration's column with an empty ledger is
    the case adoption must never wave through, since the row it would write
    claims a schema state two migrations old.
    """
    await migrated.execute("ALTER TABLE issues ADD COLUMN workspace_id UUID")

    with pytest.raises(MigrationError) as error:
        async with migrated.transaction():
            await migration_status(migrated, migrations_dir=MIGRATIONS_DIR)

    assert "unexpected column workspace_id" in str(error.value)

    # The refusal took the whole ledger prologue down with it, so there is no
    # row to check -- there is no table. Nothing was written on the way out.
    assert (
        await migrated.fetchval("SELECT to_regclass('public.schema_migrations')")
        is None
    )
