"""Migration runner tests against a fake asyncpg connection.

No database. Every statement the runner issues is recorded in order, because
order is what most of these assertions are actually about: the advisory lock
has to come first, and an already-applied migration must never reach
`execute()` at all -- asserting on a return value would not catch a runner
that answered "already applied" after running the file anyway.
"""

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.apply_migration import (
    ADVISORY_LOCK_KEY,
    CHECKSUM_MISMATCH,
    CHECKSUM_NO_FILE,
    CHECKSUM_OK,
    MigrationError,
    apply_migration,
    compute_checksum,
    migration_status,
    parse_version,
)


APPLIED_AT = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

ISSUES_SQL = "CREATE TABLE issues (id UUID PRIMARY KEY);\n"
TENANTS_SQL = "CREATE TABLE tenants (id UUID PRIMARY KEY);\n"

# The introspection answers a database left by migrations/001_issues.sql
# gives: column name -> (data_type, is_nullable, column_default), spelled as
# information_schema spells them. Transcribed from a real postgres:18 rather
# than imported from the runner, so that a typo in the runner's expectations
# is a test failure and not a shared assumption. tests/test_migration_apply_db
# is what keeps this table honest against a live server.
ISSUES_COLUMNS = {
    "id": ("uuid", "NO", "uuidv7()"),
    "title": ("text", "NO", None),
    "description": ("text", "YES", None),
    "priority": ("smallint", "NO", "0"),
    "completed_at": ("timestamp with time zone", "YES", None),
    "created_at": ("timestamp with time zone", "NO", "now()"),
    "updated_at": ("timestamp with time zone", "NO", "now()"),
}

ISSUES_INDEX = "issues_created_at_id_idx"
ISSUES_CHECK = "issues_priority_range"
ISSUES_CHECK_DEFINITION = "CHECK (((priority >= 0) AND (priority <= 4)))"
ISSUES_PRIMARY_KEY = ("id",)
ISSUES_INDEX_COLUMNS = (("created_at", True), ("id", True))


def issues_index(
    columns=ISSUES_INDEX_COLUMNS,
    is_unique=False,
    is_partial=False,
    has_expressions=False,
):
    """The index's structure: (column, descending) pairs plus its flags."""
    return {
        "columns": tuple(columns),
        "is_unique": is_unique,
        "is_partial": is_partial,
        "has_expressions": has_expressions,
    }


def issues_schema(
    columns=None,
    primary_key=ISSUES_PRIMARY_KEY,
    index=None,
    constraints=((ISSUES_CHECK, ISSUES_CHECK_DEFINITION),),
):
    """What introspection sees; defaults to an exact 001 fingerprint.

    Each refusal test overrides exactly one part, so what makes it a refusal
    is visible in the call rather than buried in a fixture. `index=()` is an
    index that does not exist, as against one shaped differently.
    """
    return {
        "columns": ISSUES_COLUMNS if columns is None else columns,
        "primary_key": tuple(primary_key),
        "index": issues_index() if index is None else index,
        "constraints": tuple(constraints),
    }


def columns_without(name: str) -> dict:
    return {key: value for key, value in ISSUES_COLUMNS.items() if key != name}


def column_default(name: str, default) -> dict:
    """`ISSUES_COLUMNS` with one column's default replaced."""
    data_type, is_nullable, _ = ISSUES_COLUMNS[name]

    return ISSUES_COLUMNS | {name: (data_type, is_nullable, default)}


class FakeConnection:
    """Records every statement in order and replays canned ledger rows.

    The ledger insert is mirrored back into `rows` so that the runner's
    re-read after adopting 001 sees the row it just wrote, as Postgres would.

    `schema` is None when the database has no `issues` table at all; a dict
    from `issues_schema()` is what introspection finds when it does.
    """

    def __init__(self, rows=None, schema=None):
        self.rows: list[dict] = list(rows or [])
        self.schema = schema
        self.statements: list[dict] = []
        self.execute_status = "CREATE TABLE"

    def _record(self, method: str, query: str, args: tuple) -> None:
        self.statements.append({"method": method, "query": query, "args": args})

    def _introspect(self, key: str):
        assert self.schema is not None, (
            "the runner introspected issues without first confirming it exists"
        )

        return self.schema[key]

    async def execute(self, query, *args):
        self._record("execute", query, args)

        if "INSERT INTO schema_migrations" in query:
            self.rows.append(
                {
                    "version": args[0],
                    "applied_at": APPLIED_AT,
                    "checksum": args[1],
                }
            )

            return "INSERT 0 1"

        return self.execute_status

    async def fetch(self, query, *args):
        self._record("fetch", query, args)

        if "schema_migrations" in query:
            return list(self.rows)

        if "information_schema.columns" in query:
            return [
                {
                    "column_name": name,
                    "data_type": data_type,
                    "is_nullable": is_nullable,
                    "column_default": default,
                }
                for name, (data_type, is_nullable, default) in self._introspect(
                    "columns"
                ).items()
            ]

        if "pg_index" in query:
            index = self._introspect("index")

            if not index:
                return []

            # One row per key column, each carrying the index-wide flags --
            # the shape the runner's query produces.
            return [
                {
                    "column_name": name,
                    "is_desc": is_desc,
                    "is_unique": index["is_unique"],
                    "is_partial": index["is_partial"],
                    "has_expressions": index["has_expressions"],
                }
                for name, is_desc in index["columns"]
            ]

        # Both constraint queries read pg_constraint, so contype decides
        # which one this is; the primary key branch has to come first.
        if "contype = 'p'" in query:
            return [
                {"attname": name} for name in self._introspect("primary_key")
            ]

        if "pg_constraint" in query:
            return [
                {"conname": name, "definition": definition}
                for name, definition in self._introspect("constraints")
            ]

        raise AssertionError(f"unexpected fetch: {query}")

    async def fetchval(self, query, *args):
        self._record("fetchval", query, args)

        assert "to_regclass" in query, f"unexpected fetchval: {query}"

        return self.schema is not None

    def queries(self) -> list[str]:
        return [statement["query"] for statement in self.statements]

    def inserts(self) -> list[tuple]:
        return [
            statement["args"]
            for statement in self.statements
            if "INSERT INTO schema_migrations" in statement["query"]
        ]

    def executed(self, sql: str) -> int:
        return sum(1 for query in self.queries() if query == sql)


def ledger_row(version: str, checksum: str) -> dict:
    return {"version": version, "applied_at": APPLIED_AT, "checksum": checksum}


def write_migration(directory: Path, name: str, sql: str) -> Path:
    path = directory / name
    path.write_text(sql, encoding="utf-8")

    return path


@pytest.fixture
def migrations_dir(tmp_path: Path) -> Path:
    """A migrations directory of our own.

    The real `migrations/001_issues.sql` is immutable and already applied;
    tests must not depend on its bytes, let alone be tempted to edit it.
    """
    directory = tmp_path / "migrations"
    directory.mkdir()

    return directory


def test_version_comes_from_the_filename_prefix():
    assert parse_version(Path("migrations/002_tenancy.sql")) == "002"
    assert parse_version(Path("migrations/001_issues.sql")) == "001"

    # Padding is preserved, so '002' and '2' cannot both reach the ledger.
    assert parse_version(Path("2_tenancy.sql")) == "2"


def test_unnumbered_filename_is_rejected():
    with pytest.raises(MigrationError):
        parse_version(Path("migrations/tenancy.sql"))


def test_checksum_is_a_stable_sha256_of_the_file_text():
    """Pinned to a literal: the ledger's value must not drift with the code."""
    assert compute_checksum("SELECT 1;\n") == (
        "b4e0497804e46e0a0b0b8c31975b062152d551bac49c3c2e80932567b4085dcd"
    )

    assert compute_checksum(TENANTS_SQL) == (
        hashlib.sha256(TENANTS_SQL.encode("utf-8")).hexdigest()
    )

    assert compute_checksum("SELECT 1;\n") != compute_checksum("SELECT 2;\n")


async def test_advisory_lock_is_the_first_statement(migrations_dir: Path):
    path = write_migration(migrations_dir, "002_tenancy.sql", TENANTS_SQL)
    connection = FakeConnection()

    await apply_migration(connection, path, migrations_dir=migrations_dir)

    first = connection.statements[0]

    assert "pg_advisory_xact_lock" in first["query"]
    assert first["args"] == (ADVISORY_LOCK_KEY,)


async def test_fresh_apply_executes_the_file_once_and_records_it(
    migrations_dir: Path,
):
    path = write_migration(migrations_dir, "002_tenancy.sql", TENANTS_SQL)
    connection = FakeConnection(rows=[ledger_row("001", compute_checksum(ISSUES_SQL))])
    write_migration(migrations_dir, "001_issues.sql", ISSUES_SQL)

    message = await apply_migration(
        connection,
        path,
        migrations_dir=migrations_dir,
    )

    assert connection.executed(TENANTS_SQL) == 1
    assert connection.inserts() == [("002", compute_checksum(TENANTS_SQL))]
    assert "002" in message


async def test_the_command_tag_is_labelled_as_the_final_statement(
    migrations_dir: Path,
):
    """asyncpg returns the last statement's tag, so that is what we claim."""
    path = write_migration(migrations_dir, "002_tenancy.sql", TENANTS_SQL)
    connection = FakeConnection()
    connection.execute_status = "CREATE INDEX"

    message = await apply_migration(
        connection,
        path,
        migrations_dir=migrations_dir,
    )

    assert message == (
        "Applied migration 002 from 002_tenancy.sql "
        "(final statement: CREATE INDEX)"
    )


async def test_a_trailing_row_count_is_not_claimed_for_the_whole_migration(
    migrations_dir: Path,
):
    """`UPDATE 7` counts one statement's rows, not the migration's."""
    path = write_migration(
        migrations_dir, "003_backfill.sql", "UPDATE issues SET x = 1;\n"
    )
    connection = FakeConnection()
    connection.execute_status = "UPDATE 7"

    message = await apply_migration(
        connection,
        path,
        migrations_dir=migrations_dir,
    )

    assert "final statement: UPDATE 7" in message
    assert "rows" not in message


async def test_applied_version_short_circuits_without_executing(
    migrations_dir: Path,
):
    path = write_migration(migrations_dir, "002_tenancy.sql", TENANTS_SQL)
    connection = FakeConnection(
        rows=[ledger_row("002", compute_checksum(TENANTS_SQL))]
    )

    message = await apply_migration(
        connection,
        path,
        migrations_dir=migrations_dir,
    )

    assert "already applied" in message
    assert connection.executed(TENANTS_SQL) == 0
    assert connection.inserts() == []


async def test_checksum_mismatch_aborts_before_executing(migrations_dir: Path):
    """An edited applied migration stops the run, naming the version."""
    write_migration(migrations_dir, "001_issues.sql", ISSUES_SQL + "-- edited\n")
    path = write_migration(migrations_dir, "002_tenancy.sql", TENANTS_SQL)

    connection = FakeConnection(
        rows=[ledger_row("001", compute_checksum(ISSUES_SQL))]
    )

    with pytest.raises(MigrationError) as error:
        await apply_migration(connection, path, migrations_dir=migrations_dir)

    assert "001" in str(error.value)
    assert connection.executed(TENANTS_SQL) == 0
    assert connection.inserts() == []


async def test_matching_checksum_does_not_abort(migrations_dir: Path):
    write_migration(migrations_dir, "001_issues.sql", ISSUES_SQL)
    path = write_migration(migrations_dir, "002_tenancy.sql", TENANTS_SQL)

    connection = FakeConnection(
        rows=[ledger_row("001", compute_checksum(ISSUES_SQL))]
    )

    await apply_migration(connection, path, migrations_dir=migrations_dir)

    assert connection.executed(TENANTS_SQL) == 1


async def test_001_is_adopted_when_the_schema_matches_its_fingerprint(
    migrations_dir: Path,
):
    write_migration(migrations_dir, "001_issues.sql", ISSUES_SQL)
    connection = FakeConnection(rows=[], schema=issues_schema())

    report = await migration_status(connection, migrations_dir=migrations_dir)

    assert connection.inserts() == [("001", compute_checksum(ISSUES_SQL))]
    assert [item.version for item in report.applied] == ["001"]
    assert report.applied[0].state == CHECKSUM_OK
    assert report.pending == ()


async def test_001_is_not_adopted_when_issues_does_not_exist(
    migrations_dir: Path,
):
    path = write_migration(migrations_dir, "001_issues.sql", ISSUES_SQL)
    connection = FakeConnection(rows=[], schema=None)

    await apply_migration(connection, path, migrations_dir=migrations_dir)

    # Adopted nothing, then applied 001 for real.
    assert connection.inserts() == [("001", compute_checksum(ISSUES_SQL))]
    assert connection.executed(ISSUES_SQL) == 1


async def test_001_is_not_adopted_when_the_ledger_already_has_rows(
    migrations_dir: Path,
):
    write_migration(migrations_dir, "001_issues.sql", ISSUES_SQL)
    connection = FakeConnection(
        rows=[ledger_row("001", compute_checksum(ISSUES_SQL))],
        schema=issues_schema(),
    )

    await migration_status(connection, migrations_dir=migrations_dir)

    assert connection.inserts() == []

    # The table probe is skipped entirely; a populated ledger is the answer.
    assert not any("to_regclass" in query for query in connection.queries())


async def test_adoption_without_the_001_file_is_an_error(migrations_dir: Path):
    connection = FakeConnection(rows=[], schema=issues_schema())

    with pytest.raises(MigrationError) as error:
        await migration_status(connection, migrations_dir=migrations_dir)

    assert "001" in str(error.value)
    assert connection.inserts() == []


async def test_adoption_refuses_when_a_column_is_missing(migrations_dir: Path):
    write_migration(migrations_dir, "001_issues.sql", ISSUES_SQL)
    connection = FakeConnection(
        rows=[],
        schema=issues_schema(columns=columns_without("description")),
    )

    with pytest.raises(MigrationError) as error:
        await migration_status(connection, migrations_dir=migrations_dir)

    assert "column description is missing" in str(error.value)
    assert connection.inserts() == []


async def test_adoption_refuses_on_an_extra_column(migrations_dir: Path):
    """A database already carrying 002's columns is the dangerous case.

    Its ledger is empty, so nothing records that 002 ran; adopting 001 here
    would write a row claiming a schema state that is two migrations old.
    """
    path = write_migration(migrations_dir, "001_issues.sql", ISSUES_SQL)
    connection = FakeConnection(
        rows=[],
        schema=issues_schema(
            columns=ISSUES_COLUMNS
            | {
                "workspace_id": ("uuid", "NO", None),
                "team_id": ("uuid", "YES", None),
            }
        ),
    )

    with pytest.raises(MigrationError) as error:
        await apply_migration(connection, path, migrations_dir=migrations_dir)

    message = str(error.value)

    assert "unexpected column team_id" in message
    assert "unexpected column workspace_id" in message

    # Not adopted -- and not silently downgraded to "001 is pending" either.
    assert connection.inserts() == []
    assert connection.executed(ISSUES_SQL) == 0


async def test_adoption_refuses_on_wrong_nullability(migrations_dir: Path):
    write_migration(migrations_dir, "001_issues.sql", ISSUES_SQL)
    connection = FakeConnection(
        rows=[],
        schema=issues_schema(
            columns=ISSUES_COLUMNS | {"title": ("text", "YES", None)}
        ),
    )

    with pytest.raises(MigrationError) as error:
        await migration_status(connection, migrations_dir=migrations_dir)

    assert "column title is nullable, expected NOT NULL" in str(error.value)
    assert connection.inserts() == []


async def test_adoption_refuses_on_a_wrong_data_type(migrations_dir: Path):
    write_migration(migrations_dir, "001_issues.sql", ISSUES_SQL)
    connection = FakeConnection(
        rows=[],
        schema=issues_schema(
            columns=ISSUES_COLUMNS | {"priority": ("integer", "NO", "0")}
        ),
    )

    with pytest.raises(MigrationError) as error:
        await migration_status(connection, migrations_dir=migrations_dir)

    assert "column priority has type integer, expected smallint" in str(
        error.value
    )
    assert connection.inserts() == []


async def test_adoption_refuses_when_there_is_no_primary_key(
    migrations_dir: Path,
):
    """Seven right columns and no key is still not the table 001 creates."""
    write_migration(migrations_dir, "001_issues.sql", ISSUES_SQL)
    connection = FakeConnection(rows=[], schema=issues_schema(primary_key=()))

    with pytest.raises(MigrationError) as error:
        await migration_status(connection, migrations_dir=migrations_dir)

    assert "primary key is missing, expected one on (id)" in str(error.value)
    assert connection.inserts() == []


async def test_adoption_refuses_when_the_primary_key_is_on_another_column(
    migrations_dir: Path,
):
    write_migration(migrations_dir, "001_issues.sql", ISSUES_SQL)
    connection = FakeConnection(
        rows=[], schema=issues_schema(primary_key=("title",))
    )

    with pytest.raises(MigrationError) as error:
        await migration_status(connection, migrations_dir=migrations_dir)

    assert "primary key is on (title), expected (id)" in str(error.value)
    assert connection.inserts() == []


async def test_adoption_refuses_on_a_composite_primary_key(
    migrations_dir: Path,
):
    """A key on (id, workspace_id) admits duplicate ids; 001's does not."""
    path = write_migration(migrations_dir, "001_issues.sql", ISSUES_SQL)
    connection = FakeConnection(
        rows=[], schema=issues_schema(primary_key=("id", "workspace_id"))
    )

    with pytest.raises(MigrationError) as error:
        await apply_migration(connection, path, migrations_dir=migrations_dir)

    assert "primary key is on (id, workspace_id), expected (id)" in str(
        error.value
    )
    assert connection.inserts() == []
    assert connection.executed(ISSUES_SQL) == 0


async def test_adoption_refuses_when_the_id_default_is_missing(
    migrations_dir: Path,
):
    """Without uuidv7() every insert that omits an id fails; 001's does not."""
    write_migration(migrations_dir, "001_issues.sql", ISSUES_SQL)
    connection = FakeConnection(
        rows=[], schema=issues_schema(columns=column_default("id", None))
    )

    with pytest.raises(MigrationError) as error:
        await migration_status(connection, migrations_dir=migrations_dir)

    assert "column id has no default, expected uuidv7()" in str(error.value)
    assert connection.inserts() == []


async def test_adoption_refuses_on_a_wrong_id_default(migrations_dir: Path):
    """gen_random_uuid() is a v4: the wrong ordering for a v7 keyset index."""
    write_migration(migrations_dir, "001_issues.sql", ISSUES_SQL)
    connection = FakeConnection(
        rows=[],
        schema=issues_schema(columns=column_default("id", "gen_random_uuid()")),
    )

    with pytest.raises(MigrationError) as error:
        await migration_status(connection, migrations_dir=migrations_dir)

    assert "column id has default gen_random_uuid(), expected uuidv7()" in str(
        error.value
    )
    assert connection.inserts() == []


async def test_adoption_refuses_on_a_wrong_priority_default(
    migrations_dir: Path,
):
    write_migration(migrations_dir, "001_issues.sql", ISSUES_SQL)
    connection = FakeConnection(
        rows=[], schema=issues_schema(columns=column_default("priority", "1"))
    )

    with pytest.raises(MigrationError) as error:
        await migration_status(connection, migrations_dir=migrations_dir)

    assert "column priority has default 1, expected 0" in str(error.value)
    assert connection.inserts() == []


@pytest.mark.parametrize("column", ["created_at", "updated_at"])
async def test_adoption_refuses_when_a_timestamp_default_is_missing(
    migrations_dir: Path, column: str
):
    """Both timestamps default to now(); neither may be left to the caller."""
    write_migration(migrations_dir, "001_issues.sql", ISSUES_SQL)
    connection = FakeConnection(
        rows=[], schema=issues_schema(columns=column_default(column, None))
    )

    with pytest.raises(MigrationError) as error:
        await migration_status(connection, migrations_dir=migrations_dir)

    assert f"column {column} has no default, expected now()" in str(error.value)
    assert connection.inserts() == []


async def test_adoption_refuses_a_default_001_does_not_create(
    migrations_dir: Path,
):
    """001 leaves description without one, so a default here is drift."""
    write_migration(migrations_dir, "001_issues.sql", ISSUES_SQL)
    connection = FakeConnection(
        rows=[],
        schema=issues_schema(columns=column_default("description", "''::text")),
    )

    with pytest.raises(MigrationError) as error:
        await migration_status(connection, migrations_dir=migrations_dir)

    assert "column description has default ''::text, which migration 001" in str(
        error.value
    )
    assert connection.inserts() == []


@pytest.mark.parametrize(
    ("column", "rendering"),
    [
        # Every one of these is how a real postgres:18 renders a default
        # equivalent to 001's; refusing any of them would block a legitimate
        # adoption, which is as much a failure as adopting a wrong schema.
        ("id", "public.uuidv7()"),
        ("id", "UUIDV7()"),
        ("priority", "(0)::smallint"),
        ("priority", "0::smallint"),
        ("priority", "(0)"),
        ("created_at", "CURRENT_TIMESTAMP"),
        ("created_at", "public.now()"),
        ("updated_at", "now( )"),
    ],
)
async def test_equivalent_default_renderings_are_adopted(
    migrations_dir: Path, column: str, rendering: str
):
    write_migration(migrations_dir, "001_issues.sql", ISSUES_SQL)
    connection = FakeConnection(
        rows=[], schema=issues_schema(columns=column_default(column, rendering))
    )

    report = await migration_status(connection, migrations_dir=migrations_dir)

    assert connection.inserts() == [("001", compute_checksum(ISSUES_SQL))]
    assert [item.version for item in report.applied] == ["001"]


async def test_adoption_refuses_when_the_index_is_absent(migrations_dir: Path):
    write_migration(migrations_dir, "001_issues.sql", ISSUES_SQL)
    connection = FakeConnection(rows=[], schema=issues_schema(index=()))

    with pytest.raises(MigrationError) as error:
        await migration_status(connection, migrations_dir=migrations_dir)

    assert "index issues_created_at_id_idx is missing" in str(error.value)
    assert connection.inserts() == []


async def test_adoption_refuses_an_index_of_the_right_name_wrong_columns(
    migrations_dir: Path,
):
    """The name is the one part of an index that carries no meaning."""
    path = write_migration(migrations_dir, "001_issues.sql", ISSUES_SQL)
    connection = FakeConnection(
        rows=[],
        schema=issues_schema(index=issues_index(columns=(("title", False),))),
    )

    with pytest.raises(MigrationError) as error:
        await apply_migration(connection, path, migrations_dir=migrations_dir)

    assert (
        "index issues_created_at_id_idx is on (title ASC), expected "
        "(created_at DESC, id DESC)"
    ) in str(error.value)
    assert connection.inserts() == []
    assert connection.executed(ISSUES_SQL) == 0


async def test_adoption_refuses_an_index_with_the_wrong_direction(
    migrations_dir: Path,
):
    """Keyset pagination reads this index backwards if created_at is ASC."""
    write_migration(migrations_dir, "001_issues.sql", ISSUES_SQL)
    connection = FakeConnection(
        rows=[],
        schema=issues_schema(
            index=issues_index(columns=(("created_at", False), ("id", True)))
        ),
    )

    with pytest.raises(MigrationError) as error:
        await migration_status(connection, migrations_dir=migrations_dir)

    assert (
        "index issues_created_at_id_idx is on (created_at ASC, id DESC), "
        "expected (created_at DESC, id DESC)"
    ) in str(error.value)
    assert connection.inserts() == []


async def test_adoption_refuses_a_unique_or_partial_index(
    migrations_dir: Path,
):
    """Either one rejects or hides rows that 001's index does not."""
    write_migration(migrations_dir, "001_issues.sql", ISSUES_SQL)
    connection = FakeConnection(
        rows=[],
        schema=issues_schema(index=issues_index(is_unique=True, is_partial=True)),
    )

    with pytest.raises(MigrationError) as error:
        await migration_status(connection, migrations_dir=migrations_dir)

    message = str(error.value)

    assert "is UNIQUE, expected a non-unique index" in message
    assert "is partial (it has a WHERE clause)" in message
    assert connection.inserts() == []


async def test_adoption_refuses_when_the_check_constraint_is_absent(
    migrations_dir: Path,
):
    write_migration(migrations_dir, "001_issues.sql", ISSUES_SQL)
    connection = FakeConnection(rows=[], schema=issues_schema(constraints=()))

    with pytest.raises(MigrationError) as error:
        await migration_status(connection, migrations_dir=migrations_dir)

    assert "check constraint issues_priority_range is missing" in str(
        error.value
    )
    assert connection.inserts() == []


async def test_adoption_refuses_the_right_constraint_name_wrong_expression(
    migrations_dir: Path,
):
    """`<= 9` under 001's name admits priorities the application cannot mean."""
    path = write_migration(migrations_dir, "001_issues.sql", ISSUES_SQL)
    connection = FakeConnection(
        rows=[],
        schema=issues_schema(
            constraints=(
                (ISSUES_CHECK, "CHECK (((priority >= 0) AND (priority <= 9)))"),
            )
        ),
    )

    with pytest.raises(MigrationError) as error:
        await apply_migration(connection, path, migrations_dir=migrations_dir)

    assert (
        "check constraint issues_priority_range is "
        "CHECK (((priority >= 0) AND (priority <= 9))), expected "
        "CHECK (((priority >= 0) AND (priority <= 4)))"
    ) in str(error.value)
    assert connection.inserts() == []
    assert connection.executed(ISSUES_SQL) == 0


async def test_the_check_constraint_is_compared_past_case_and_whitespace(
    migrations_dir: Path,
):
    """Same rule, different rendering, is not a discrepancy."""
    write_migration(migrations_dir, "001_issues.sql", ISSUES_SQL)
    connection = FakeConnection(
        rows=[],
        schema=issues_schema(
            constraints=(
                (
                    ISSUES_CHECK,
                    "check (((priority >= 0)\n   and (priority <= 4)))",
                ),
            )
        ),
    )

    await migration_status(connection, migrations_dir=migrations_dir)

    assert connection.inserts() == [("001", compute_checksum(ISSUES_SQL))]


async def test_adoption_refuses_a_check_constraint_001_does_not_create(
    migrations_dir: Path,
):
    """An extra CHECK is DDL that ran outside the ledger, like an extra column."""
    write_migration(migrations_dir, "001_issues.sql", ISSUES_SQL)
    connection = FakeConnection(
        rows=[],
        schema=issues_schema(
            constraints=(
                (ISSUES_CHECK, ISSUES_CHECK_DEFINITION),
                ("issues_title_length", "CHECK ((length(title) > 0))"),
            )
        ),
    )

    with pytest.raises(MigrationError) as error:
        await migration_status(connection, migrations_dir=migrations_dir)

    assert "unexpected check constraint issues_title_length" in str(error.value)
    assert connection.inserts() == []


async def test_the_refusal_names_every_discrepancy_and_says_what_to_do(
    migrations_dir: Path,
):
    """One run has to be enough to reconcile by hand, so list them all."""
    write_migration(migrations_dir, "001_issues.sql", ISSUES_SQL)
    connection = FakeConnection(
        rows=[],
        schema=issues_schema(
            columns=columns_without("completed_at")
            | {"archived_at": ("timestamp with time zone", "YES", None)},
            primary_key=(),
            index=(),
        ),
    )

    with pytest.raises(MigrationError) as error:
        await migration_status(connection, migrations_dir=migrations_dir)

    message = str(error.value)

    assert "column completed_at is missing" in message
    assert "unexpected column archived_at" in message
    assert "primary key is missing" in message
    assert "index issues_created_at_id_idx is missing" in message
    assert "public.issues" in message
    assert "Reconcile it by hand" in message
    assert connection.inserts() == []


async def test_status_lists_pending_files_and_verifies_applied_ones(
    migrations_dir: Path,
):
    write_migration(migrations_dir, "001_issues.sql", ISSUES_SQL)
    write_migration(migrations_dir, "002_tenancy.sql", TENANTS_SQL)
    write_migration(migrations_dir, "003_teams.sql", "SELECT 1;\n")

    connection = FakeConnection(
        rows=[
            ledger_row("001", compute_checksum(ISSUES_SQL)),
            ledger_row("002", compute_checksum(TENANTS_SQL)),
        ]
    )

    report = await migration_status(connection, migrations_dir=migrations_dir)

    assert [item.state for item in report.applied] == [CHECKSUM_OK, CHECKSUM_OK]
    assert [path.name for path in report.pending] == ["003_teams.sql"]
    assert not report.has_mismatch


async def test_status_reports_a_mismatch_instead_of_raising(
    migrations_dir: Path,
):
    """Diagnosis is what status is for, so it renders the bad row."""
    write_migration(migrations_dir, "001_issues.sql", ISSUES_SQL + "-- edited\n")

    connection = FakeConnection(
        rows=[ledger_row("001", compute_checksum(ISSUES_SQL))]
    )

    report = await migration_status(connection, migrations_dir=migrations_dir)

    assert report.applied[0].state == CHECKSUM_MISMATCH
    assert report.has_mismatch


async def test_applied_migration_with_no_file_is_reported_not_failed(
    migrations_dir: Path,
):
    """An older branch legitimately lacks a file the database already has."""
    connection = FakeConnection(rows=[ledger_row("009", "whatever")])

    report = await migration_status(connection, migrations_dir=migrations_dir)

    assert report.applied[0].state == CHECKSUM_NO_FILE
    assert not report.has_mismatch


async def test_duplicate_version_in_the_directory_is_rejected(
    migrations_dir: Path,
):
    write_migration(migrations_dir, "002_tenancy.sql", TENANTS_SQL)
    path = write_migration(migrations_dir, "002_teams.sql", "SELECT 1;\n")

    connection = FakeConnection()

    with pytest.raises(MigrationError) as error:
        await apply_migration(connection, path, migrations_dir=migrations_dir)

    assert "002" in str(error.value)
