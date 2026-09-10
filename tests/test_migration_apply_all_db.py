"""`--all` applies the whole directory in one process, and still atomically.

THE DEFECT THIS FIXES was a start-up time, not a wrong answer. The container
ran the equivalent of

    for file in migrations/*.sql; do python -m scripts.apply_migration "$file"; done

which is 32 interpreter starts before uvicorn is launched at all. Measured
against the real database from a fast development machine: 14.6s for the loop,
of which 13.8s (432ms x 32) was interpreter start and imports and under a
second was database work -- nearly all of it "already applied, do nothing".
That overhead is CPU-bound and a Render free instance has 0.1 CPU, so the same
work runs into minutes there, on every boot and every wake from sleep, for a
schema that was already current. `/healthz` cannot answer until it finishes.

Collapsing the processes alone was not enough: `apply_migration` re-prepares
the ledger and re-verifies every checksum on each call, which is correct for
one file and quadratic over a directory. Hoisting that took the same run from
10.5s to 1.3s.

So the speed is the reason and the SAFETY is what these tests are about. What
must survive is the property the per-file boundary existed for: each migration
in its own transaction, so a failure part-way leaves everything before it
committed and the failing one rolled back whole. A version of `--all` that
wrapped the directory in one transaction would be faster still and would lose
exactly that.

Marked `db`: deselected by default, skipped when Docker is unreachable.
"""

from pathlib import Path

import asyncpg
import pytest

from scripts.apply_migration import (
    MigrationError,
    _run_apply_all,
    migration_status,
)

from tests.conftest import reset_schema


pytestmark = pytest.mark.db

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"

EXPECTED_COUNT = len(sorted(MIGRATIONS_DIR.glob("*.sql")))


@pytest.fixture
async def empty(postgres_dsn, monkeypatch):
    """A database with no schema at all, and the runner aimed at it.

    `_run_apply_all` opens its own connection through `_connect`, which reads
    `DATABASE_URL` from settings -- so the fixture points settings at the
    throwaway container rather than passing a connection in. That is also what
    makes this a test of the ENTRY POINT the Dockerfile calls, rather than of
    a function assembled differently here.
    """
    import app.config

    monkeypatch.setenv("DATABASE_URL", postgres_dsn)
    app.config.get_settings.cache_clear()

    connection = await asyncpg.connect(postgres_dsn)

    try:
        await reset_schema(connection)
        yield connection
    finally:
        await connection.close()
        app.config.get_settings.cache_clear()


async def test_one_invocation_applies_every_migration(empty):
    """The whole directory, from nothing, in a single process."""
    await _run_apply_all()

    async with empty.transaction():
        report = await migration_status(empty, migrations_dir=MIGRATIONS_DIR)

    assert len(report.applied) == EXPECTED_COUNT
    assert report.pending == ()
    assert not report.has_mismatch

    # Not merely a ledger full of rows: the schema those rows claim.
    assert await empty.fetchval("SELECT to_regclass('public.issues')") is not None
    assert await empty.fetchval("SELECT to_regclass('public.workspaces')") is not None


async def test_a_second_invocation_is_a_no_op(empty, capsys):
    """The case every boot actually takes, and the one that was expensive.

    A deployment whose schema is current re-runs this on every restart. It
    must apply nothing, write nothing, and say so -- and it is the path whose
    cost used to be 32 interpreter starts.
    """
    await _run_apply_all()

    async with empty.transaction():
        before = await empty.fetchval("SELECT max(applied_at) FROM schema_migrations")

    capsys.readouterr()

    await _run_apply_all()

    assert "nothing to do" in capsys.readouterr().out

    async with empty.transaction():
        after = await empty.fetchval("SELECT max(applied_at) FROM schema_migrations")
        count = await empty.fetchval("SELECT count(*) FROM schema_migrations")

    # Same rows, untouched: a re-run that re-stamped `applied_at` would be
    # re-executing migrations while reporting that it had not.
    assert after == before
    assert count == EXPECTED_COUNT


async def test_a_failure_part_way_keeps_everything_before_it(empty, monkeypatch):
    """The property that must not be traded for the speed.

    Each migration keeps its OWN transaction, so a failure leaves the ones
    before it applied and committed and the failing one rolled back whole --
    which is what makes a re-run resume rather than start over. Wrapping the
    directory in a single transaction would be faster and would lose this.

    The failure is injected by corrupting the SQL of one file as it is read,
    which is the closest thing to a migration that genuinely does not apply.
    """
    import scripts.apply_migration as runner

    real_read = runner.read_migration
    fail_at = "003"

    def read(path: Path) -> str:
        if path.name.startswith(fail_at):
            return "SELECT this_function_does_not_exist();"

        return real_read(path)

    monkeypatch.setattr(runner, "read_migration", read)

    with pytest.raises(asyncpg.PostgresError):
        await _run_apply_all()

    monkeypatch.setattr(runner, "read_migration", real_read)

    async with empty.transaction():
        report = await migration_status(empty, migrations_dir=MIGRATIONS_DIR)

    applied = {item.version for item in report.applied}

    # Everything before the failure committed; the failure itself did not.
    assert "001" in applied
    assert "002" in applied
    assert fail_at not in applied

    # And the schema agrees with the ledger -- 002's tables are really there,
    # and 003's are not.
    assert await empty.fetchval("SELECT to_regclass('public.workspaces')") is not None
    assert await empty.fetchval("SELECT to_regclass('public.users')") is None


async def test_an_applied_migration_that_changed_on_disk_stops_the_run(
    empty, monkeypatch
):
    """The immutability check survives being hoisted out of the per-file path.

    It used to run inside `apply_migration`, once per file. `--all` performs it
    once for the whole run, so this asserts it still refuses -- an applied
    migration whose file has changed is not something to serve requests over,
    and the speed-up must not have bought silence.
    """
    await _run_apply_all()

    import scripts.apply_migration as runner

    real_read = runner.read_migration

    def read(path: Path) -> str:
        if path.name.startswith("002"):
            return real_read(path) + "\n-- edited after the fact\n"

        return real_read(path)

    monkeypatch.setattr(runner, "read_migration", read)

    with pytest.raises(MigrationError) as error:
        await _run_apply_all()

    assert "immutable" in str(error.value)
