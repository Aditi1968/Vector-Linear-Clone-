"""Migration runner with a ledger.

The previous runner executed whatever file it was handed, every time it was
handed it. Nothing recorded what had already run, so re-applying an applied
migration looked like a perfectly ordinary command, and `CLAUDE.md`'s "001 is
already applied and is immutable" rule was enforced by nothing but attention.

This runner keeps a `schema_migrations` ledger and refuses to execute a
version twice. It also stores the sha256 of each file as applied, which turns
"never modify an already-applied migration" from a convention into a check:
an edited file no longer matches its recorded checksum and the runner stops
before touching the database.

The ledger did not exist when 001 was applied to production, so the first run
against an existing database adopts 001 instead of reporting it pending --
otherwise the obvious next step is to re-apply a migration that already ran.
Adoption is not taken on trust: the runner fingerprints the live `issues`
table against the columns, key, constraint and index 001 creates, and refuses
to adopt anything else. A ledger row is a claim about the schema, and writing
one for a table nobody verified would put a lie at the root of every later
decision.

There is no `psql` or `pg_dump` on the development machines, so `--status` is
also the only schema inspection tool this project has.
"""

import asyncio
import hashlib
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

import asyncpg

from app.config import get_settings


MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"

# Any 64-bit constant works, as long as every runner picks the same one.
# Changing it silently un-serializes concurrent runs, so it never changes.
ADVISORY_LOCK_KEY = 0x56454354

INITIAL_VERSION = "001"
INITIAL_MIGRATION_FILENAME = "001_issues.sql"

VERSION_PATTERN = re.compile(r"^(\d+)_")

CHECKSUM_OK = "ok"
CHECKSUM_MISMATCH = "MISMATCH"
CHECKSUM_NO_FILE = "no file"

ChecksumState = Literal["ok", "MISMATCH", "no file"]

# `IF NOT EXISTS` belongs here and nowhere else: this is runner code that has
# to cope with any starting state. Migration files stay free of defensive DDL.
CREATE_LEDGER_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    checksum TEXT NOT NULL
)
"""

SELECT_APPLIED_SQL = """
SELECT version, applied_at, checksum
FROM schema_migrations
ORDER BY version
"""

INSERT_APPLIED_SQL = """
INSERT INTO schema_migrations (version, checksum)
VALUES ($1, $2)
"""

INITIAL_TABLE = "public.issues"
INITIAL_INDEX = "issues_created_at_id_idx"
INITIAL_CHECK_CONSTRAINT = "issues_priority_range"

# In key order: a primary key over the same columns in a different order is
# a different key, and neither is the one 001 declares.
INITIAL_PRIMARY_KEY = ("id",)

# The fingerprint of the table 001 creates: column name -> (data_type as
# information_schema spells it, nullable). Adoption compares the live table
# against exactly this dict, in both directions -- an extra column is as
# disqualifying as a missing one, because it means DDL ran outside the
# ledger and the database is not the state 001 leaves behind.
INITIAL_COLUMNS: dict[str, tuple[str, bool]] = {
    "id": ("uuid", False),
    "title": ("text", False),
    "description": ("text", True),
    "priority": ("smallint", False),
    "completed_at": ("timestamp with time zone", True),
    "created_at": ("timestamp with time zone", False),
    "updated_at": ("timestamp with time zone", False),
}

# Accepted *normalized* defaults per column (see `_normalize_default`). A
# column absent from this dict must have no default at all: a default 001
# does not create is drift like any other. Same columns, same types, same
# nullability and a different default is a different table -- one where a
# row 001's schema would have stamped with uuidv7() gets something else.
INITIAL_DEFAULTS: dict[str, frozenset[str]] = {
    "id": frozenset({"uuidv7()"}),
    "priority": frozenset({"0"}),
    "created_at": frozenset({"now()"}),
    "updated_at": frozenset({"now()"}),
}

# The index's structure, in key order: (column, descending). Checked instead
# of the name, because the name is the one part of an index that carries no
# meaning -- `issues_created_at_id_idx` defined on (title) would otherwise
# fingerprint as 001's keyset index.
INITIAL_INDEX_COLUMNS: tuple[tuple[str, bool], ...] = (
    ("created_at", True),
    ("id", True),
)

# PostgreSQL rewrites BETWEEN, so `CHECK (priority BETWEEN 0 AND 4)` comes
# back out of the catalog in this form. Compared after normalization, so the
# spacing here is documentation rather than load-bearing.
INITIAL_CHECK_DEFINITION = "CHECK (((priority >= 0) AND (priority <= 4)))"

DEFAULT_CAST = re.compile(r"::[a-z0-9_.]+")

# PostgreSQL stores CURRENT_TIMESTAMP as written instead of folding it to
# now(); they are the same function, so one is reduced to the other.
DEFAULT_ALIASES = {"current_timestamp": "now()"}

TABLE_EXISTS_SQL = "SELECT to_regclass('public.issues') IS NOT NULL"

# Everything below is read-only introspection. Adoption inspects; it never
# repairs, because a schema nobody can explain is not a schema to write to.
ISSUES_COLUMNS_SQL = """
SELECT column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_schema = 'public' AND table_name = 'issues'
"""

# The index by structure rather than by name: its columns in key order, each
# column's direction, and the three properties that would make it a
# different index even on the right columns. `indkey` and `indoption` are
# int2vectors -- indkey is cast so that unnest accepts it, indoption is left
# alone and subscripted 0-based, which is how PostgreSQL numbers it.
ISSUES_INDEX_SQL = """
SELECT
    pg_attribute.attname AS column_name,
    (pg_index.indoption[key_column.ord - 1] & 1) = 1 AS is_desc,
    pg_index.indisunique AS is_unique,
    pg_index.indpred IS NOT NULL AS is_partial,
    pg_index.indexprs IS NOT NULL AS has_expressions
FROM pg_index
JOIN pg_class ON pg_class.oid = pg_index.indexrelid
CROSS JOIN LATERAL
    unnest(pg_index.indkey::smallint[]) WITH ORDINALITY AS key_column(attnum, ord)
LEFT JOIN pg_attribute
    ON pg_attribute.attrelid = pg_index.indrelid
    AND pg_attribute.attnum = key_column.attnum
WHERE pg_index.indrelid = 'public.issues'::regclass
    AND pg_class.relname = 'issues_created_at_id_idx'
ORDER BY key_column.ord
"""

# `contype = 'c'` is doing real work on PostgreSQL 18, where NOT NULL
# constraints are catalogued too (as `contype = 'n'`); without it every
# NOT NULL column would arrive here as an unexpected check constraint.
ISSUES_CHECK_CONSTRAINTS_SQL = """
SELECT conname, pg_get_constraintdef(oid) AS definition
FROM pg_constraint
WHERE conrelid = 'public.issues'::regclass AND contype = 'c'
"""

# Returns the primary key's columns in key order, and no rows at all when the
# table has no primary key. Naming the columns is the point: "primary key is
# on (title), expected (id)" tells an operator what to look at, where
# "primary key mismatch" only tells them to go looking.
ISSUES_PRIMARY_KEY_SQL = """
SELECT pg_attribute.attname
FROM pg_constraint
CROSS JOIN LATERAL
    unnest(pg_constraint.conkey) WITH ORDINALITY AS key_column(attnum, ord)
JOIN pg_attribute
    ON pg_attribute.attrelid = pg_constraint.conrelid
    AND pg_attribute.attnum = key_column.attnum
WHERE pg_constraint.conrelid = 'public.issues'::regclass
    AND pg_constraint.contype = 'p'
ORDER BY key_column.ord
"""

USAGE = (
    "Usage:\n"
    "  python -m scripts.apply_migration <migration-file>\n"
    "  python -m scripts.apply_migration --status"
)


class MigrationError(RuntimeError):
    """A refusal to proceed, safe to show the operator verbatim."""


@dataclass(frozen=True)
class AppliedMigration:
    version: str
    applied_at: datetime
    checksum: str


@dataclass(frozen=True)
class VerifiedMigration:
    version: str
    applied_at: datetime
    state: ChecksumState


@dataclass(frozen=True)
class StatusReport:
    applied: tuple[VerifiedMigration, ...]
    pending: tuple[Path, ...]

    @property
    def has_mismatch(self) -> bool:
        return any(item.state == CHECKSUM_MISMATCH for item in self.applied)


def parse_version(path: Path) -> str:
    """`migrations/002_tenancy.sql` -> `'002'`.

    The zero padding is kept: the ledger key is the filename prefix exactly
    as written, so `'002'` and `'2'` can never both appear.
    """
    match = VERSION_PATTERN.match(path.name)

    if match is None:
        raise MigrationError(
            f"Migration filename must start with a numeric version: {path.name}"
        )

    return match.group(1)


def compute_checksum(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def read_migration(path: Path) -> str:
    """Read a migration as text, not bytes.

    `read_text` normalizes line endings, so a CRLF checkout on Windows and an
    LF checkout on CI produce the same checksum for the same migration. A
    byte-level digest would flag every Windows clone as tampered.
    """
    return path.read_text(encoding="utf-8")


def discover_migrations(migrations_dir: Path) -> dict[str, Path]:
    """Map version -> file for every numbered `.sql` file in the directory."""
    found: dict[str, Path] = {}

    for path in sorted(migrations_dir.glob("*.sql")):
        version = parse_version(path)

        if version in found:
            raise MigrationError(
                f"Duplicate migration version {version}: "
                f"{found[version].name} and {path.name}"
            )

        found[version] = path

    return found


def verify_checksums(
    applied: list[AppliedMigration],
    files: dict[str, Path],
) -> list[VerifiedMigration]:
    """Recompute the digest of every applied version still present on disk.

    A version with no file is reported rather than failed: checking out an
    older branch legitimately hides a migration that the database has.
    """
    results: list[VerifiedMigration] = []

    for migration in applied:
        path = files.get(migration.version)

        if path is None:
            state: ChecksumState = CHECKSUM_NO_FILE
        elif compute_checksum(read_migration(path)) == migration.checksum:
            state = CHECKSUM_OK
        else:
            state = CHECKSUM_MISMATCH

        results.append(
            VerifiedMigration(
                version=migration.version,
                applied_at=migration.applied_at,
                state=state,
            )
        )

    return results


def _raise_on_mismatch(results: list[VerifiedMigration]) -> None:
    mismatched = [item.version for item in results if item.state == CHECKSUM_MISMATCH]

    if mismatched:
        raise MigrationError(
            "Applied migration(s) changed on disk since they were applied: "
            f"{', '.join(mismatched)}. An applied migration is immutable -- "
            "restore the file and add a new numbered migration instead."
        )


def _nullability(nullable: bool) -> str:
    return "nullable" if nullable else "NOT NULL"


def _normalize_sql(text: str) -> str:
    """Fold case and whitespace, which is all `pg_get_constraintdef` varies."""
    return " ".join(text.split()).lower()


def _strip_outer_parens(expression: str) -> str:
    """`((0))` -> `0`, leaving `(a)+(b)` alone."""
    while expression.startswith("(") and expression.endswith(")"):
        depth = 0

        for position, character in enumerate(expression):
            if character == "(":
                depth += 1
            elif character == ")":
                depth -= 1

                # Closed before the end, so these are two adjacent groups
                # rather than one wrapper around the whole expression.
                if depth == 0 and position < len(expression) - 1:
                    return expression

        expression = expression[1:-1]

    return expression


def _normalize_default(expression: str | None) -> str | None:
    """Reduce a catalogued default to a form worth comparing.

    This is normalized-catalog comparison, not semantic proof about
    arbitrary expressions. It folds case, whitespace, `public.`
    qualification, casts and redundant parens, and treats CURRENT_TIMESTAMP
    as now() -- PostgreSQL stores that one as written rather than folding it.

    That is exact for the four defaults 001 declares: the catalog renders
    both `0` and `0::smallint` as `(0)::smallint`, which reduces to `0`
    here. For anything else it is conservative in the safe direction -- an
    expression it cannot reduce fails to match, and the schema is refused
    rather than adopted.
    """
    if expression is None:
        return None

    reduced = "".join(expression.split()).lower()

    if not reduced:
        return None

    reduced = reduced.replace("public.", "")
    reduced = DEFAULT_CAST.sub("", reduced)
    reduced = _strip_outer_parens(reduced)

    return DEFAULT_ALIASES.get(reduced, reduced)


def _default_discrepancies(name: str, raw: str | None) -> list[str]:
    """How the live default for one column differs from what 001 gives it."""
    accepted = INITIAL_DEFAULTS.get(name, frozenset())
    default = _normalize_default(raw)

    if default in accepted or (not accepted and default is None):
        return []

    if not accepted:
        return [
            f"column {name} has default {raw}, which migration "
            f"{INITIAL_VERSION} does not create"
        ]

    expected = " or ".join(sorted(accepted))

    if default is None:
        return [f"column {name} has no default, expected {expected}"]

    return [f"column {name} has default {raw}, expected {expected}"]


def _primary_key_discrepancies(rows) -> list[str]:
    """Check the primary key is on exactly `id`.

    "A primary key exists" is not the invariant: a composite key, or one on
    another column, is as far from what 001 declares as none at all, and
    adopting a table whose `id` is not the key would leave duplicate ids
    possible under a ledger row claiming otherwise.
    """
    columns = tuple(row["attname"] for row in rows)
    expected = ", ".join(INITIAL_PRIMARY_KEY)

    if not columns:
        return [f"primary key is missing, expected one on ({expected})"]

    if columns != INITIAL_PRIMARY_KEY:
        return [f"primary key is on ({', '.join(columns)}), expected ({expected})"]

    return []


def _column_discrepancies(rows) -> list[str]:
    """Compare the live column set against `INITIAL_COLUMNS`, both ways.

    Every difference is collected rather than the first one raised: an
    operator reconciling a database by hand needs the whole list, not one
    discrepancy per run.
    """
    found = {row["column_name"]: row for row in rows}
    problems: list[str] = []

    for name in sorted(set(INITIAL_COLUMNS) - set(found)):
        problems.append(f"column {name} is missing")

    for name in sorted(set(found) - set(INITIAL_COLUMNS)):
        problems.append(
            f"unexpected column {name} ({found[name]['data_type']}), which "
            f"migration {INITIAL_VERSION} does not create"
        )

    for name in sorted(set(INITIAL_COLUMNS) & set(found)):
        expected_type, expected_nullable = INITIAL_COLUMNS[name]
        row = found[name]

        if row["data_type"] != expected_type:
            problems.append(
                f"column {name} has type {row['data_type']}, expected {expected_type}"
            )

        nullable = row["is_nullable"] == "YES"

        if nullable != expected_nullable:
            problems.append(
                f"column {name} is {_nullability(nullable)}, "
                f"expected {_nullability(expected_nullable)}"
            )

        problems.extend(_default_discrepancies(name, row["column_default"]))

    return problems


def _render_index_columns(columns) -> str:
    return ", ".join(
        f"{name or '<expression>'} {'DESC' if is_desc else 'ASC'}"
        for name, is_desc in columns
    )


def _index_discrepancies(rows) -> list[str]:
    """Compare the keyset index by structure; its name proves nothing.

    An index called `issues_created_at_id_idx` can be on any columns, in
    either direction, unique, or partial -- each of which is a different
    index from the one `IssueRepository.list` was written against.
    """
    if not rows:
        return [f"index {INITIAL_INDEX} is missing"]

    problems: list[str] = []
    index = rows[0]

    if index["is_unique"]:
        problems.append(f"index {INITIAL_INDEX} is UNIQUE, expected a non-unique index")

    if index["is_partial"]:
        problems.append(
            f"index {INITIAL_INDEX} is partial (it has a WHERE clause), "
            "expected an unconditional index"
        )

    if index["has_expressions"]:
        problems.append(
            f"index {INITIAL_INDEX} is over an expression, expected plain columns"
        )

    columns = tuple((row["column_name"], bool(row["is_desc"])) for row in rows)

    if columns != INITIAL_INDEX_COLUMNS:
        problems.append(
            f"index {INITIAL_INDEX} is on "
            f"({_render_index_columns(columns)}), expected "
            f"({_render_index_columns(INITIAL_INDEX_COLUMNS)})"
        )

    return problems


def _check_constraint_discrepancies(rows) -> list[str]:
    """Compare the check constraint by definition, again not by name.

    `CHECK (priority BETWEEN 0 AND 9)` under the name 001 uses admits
    priorities the application has no meaning for, so the name matching is
    not evidence that the rule does.
    """
    problems: list[str] = []
    found = {row["conname"]: row["definition"] for row in rows}
    definition = found.pop(INITIAL_CHECK_CONSTRAINT, None)

    if definition is None:
        problems.append(f"check constraint {INITIAL_CHECK_CONSTRAINT} is missing")
    elif _normalize_sql(definition) != _normalize_sql(INITIAL_CHECK_DEFINITION):
        problems.append(
            f"check constraint {INITIAL_CHECK_CONSTRAINT} is {definition}, "
            f"expected {INITIAL_CHECK_DEFINITION}"
        )

    for name in sorted(found):
        problems.append(
            f"unexpected check constraint {name} ({found[name]}), which "
            f"migration {INITIAL_VERSION} does not create"
        )

    return problems


async def _initial_schema_discrepancies(
    connection: asyncpg.Connection,
) -> list[str]:
    """Every way the existing `issues` table differs from what 001 creates."""
    columns = await connection.fetch(ISSUES_COLUMNS_SQL)
    primary_key = await connection.fetch(ISSUES_PRIMARY_KEY_SQL)
    index = await connection.fetch(ISSUES_INDEX_SQL)
    constraints = await connection.fetch(ISSUES_CHECK_CONSTRAINTS_SQL)

    return [
        *_column_discrepancies(columns),
        *_primary_key_discrepancies(primary_key),
        *_index_discrepancies(index),
        *_check_constraint_discrepancies(constraints),
    ]


async def _fetch_applied(
    connection: asyncpg.Connection,
) -> list[AppliedMigration]:
    rows = await connection.fetch(SELECT_APPLIED_SQL)

    return [
        AppliedMigration(
            version=row["version"],
            applied_at=row["applied_at"],
            checksum=row["checksum"],
        )
        for row in rows
    ]


async def _adopt_initial_migration(
    connection: asyncpg.Connection,
    migrations_dir: Path,
) -> bool:
    """Record 001 as applied when the ledger is new but `issues` matches 001.

    001 reached production before this ledger did. Without adoption the first
    `--status` on that database reports 001 pending, and applying it either
    fails on the existing table or, on a partially matching schema, succeeds
    and leaves the database in a state nobody designed.

    The table's *name* is not the evidence -- its shape is. A table called
    `issues` that 001 did not create can be anything: half-built, from
    another project, or already carrying a later migration's columns. Only an
    exact fingerprint match is adopted; anything else stops the run, because
    an unknown schema with an empty ledger is a question for an operator and
    applying 001 over it would make it worse.
    """
    issues_exists = await connection.fetchval(TABLE_EXISTS_SQL)

    if not issues_exists:
        return False

    problems = await _initial_schema_discrepancies(connection)

    if problems:
        detail = "\n".join(f"  - {problem}" for problem in problems)

        raise MigrationError(
            f"Refusing to adopt migration {INITIAL_VERSION}: {INITIAL_TABLE} "
            f"exists but does not match the schema {INITIAL_VERSION} "
            f"creates:\n{detail}\n"
            "The ledger is empty, so this database's history is unknown. "
            "Reconcile it by hand -- establish what has actually been applied "
            "and insert the matching schema_migrations row(s) -- rather than "
            "letting the runner guess."
        )

    path = migrations_dir / INITIAL_MIGRATION_FILENAME

    if not path.is_file():
        # Adopting without a checksum would record a row we can never verify,
        # so stop instead: this is a broken checkout, not a database problem.
        raise MigrationError(
            f"Cannot adopt migration {INITIAL_VERSION}: {path} is missing, "
            "but the issues table exists."
        )

    await connection.execute(
        INSERT_APPLIED_SQL,
        INITIAL_VERSION,
        compute_checksum(read_migration(path)),
    )

    return True


async def _prepare_ledger(
    connection: asyncpg.Connection,
    migrations_dir: Path,
) -> list[AppliedMigration]:
    """Lock, ensure the ledger exists, adopt 001, and read what is applied.

    Order matters. The lock is the first statement so that a second runner
    blocks before it can observe a half-built ledger, and it is a *xact* lock
    so the caller's commit or rollback releases it without any cleanup path.
    """
    await connection.execute(
        "SELECT pg_advisory_xact_lock($1)",
        ADVISORY_LOCK_KEY,
    )
    await connection.execute(CREATE_LEDGER_SQL)

    applied = await _fetch_applied(connection)

    if not applied and await _adopt_initial_migration(connection, migrations_dir):
        applied = await _fetch_applied(connection)

    return applied


async def apply_migration(
    connection: asyncpg.Connection,
    path: Path,
    *,
    migrations_dir: Path = MIGRATIONS_DIR,
) -> str:
    """Apply one migration file, returning a line describing the outcome.

    The caller owns the transaction and every statement below must land in
    it: the advisory lock has to hold until commit, and a migration that
    fails halfway has to take its ledger row down with it.
    """
    version = parse_version(path)
    sql = read_migration(path)

    applied = await _prepare_ledger(connection, migrations_dir)
    _raise_on_mismatch(verify_checksums(applied, discover_migrations(migrations_dir)))

    if any(migration.version == version for migration in applied):
        return f"Migration {version} is already applied; nothing to do."

    # No parameters, so asyncpg uses the simple query protocol -- which is
    # what allows a file to hold several statements. They join the caller's
    # transaction, so migration files must contain no BEGIN or COMMIT.
    status = await connection.execute(sql)

    await connection.execute(INSERT_APPLIED_SQL, version, compute_checksum(sql))

    # asyncpg returns one command tag for the whole file: the last
    # statement's. Labelling it as such is the only honest reading of it --
    # a file ending in CREATE INDEX carries no count at all, and a trailing
    # UPDATE's count describes that statement, not the migration. Splitting
    # the file to count each statement would need a real SQL parser to
    # survive dollar-quoted bodies and string literals, so it is not done.
    detail = f" (final statement: {status})" if status else ""

    return f"Applied migration {version} from {path.name}{detail}"


async def migration_status(
    connection: asyncpg.Connection,
    *,
    migrations_dir: Path = MIGRATIONS_DIR,
) -> StatusReport:
    """Report the ledger and any migration files not yet in it.

    Unlike `apply_migration` this reports a checksum mismatch rather than
    raising on it -- diagnosing a mismatch is exactly what an operator runs
    this for, and a command that only says "aborted" cannot say which
    version, when it was applied, or what else is pending.
    """
    applied = await _prepare_ledger(connection, migrations_dir)
    files = discover_migrations(migrations_dir)

    applied_versions = {migration.version for migration in applied}
    pending = tuple(
        files[version] for version in sorted(files) if version not in applied_versions
    )

    return StatusReport(
        applied=tuple(verify_checksums(applied, files)),
        pending=pending,
    )


def render_status(report: StatusReport) -> str:
    lines: list[str] = []

    if report.applied:
        lines.append(f"{'VERSION':<10}{'APPLIED AT':<28}CHECKSUM")

        for item in report.applied:
            applied_at = item.applied_at.isoformat(sep=" ", timespec="seconds")
            lines.append(f"{item.version:<10}{applied_at:<28}{item.state}")
    else:
        lines.append("No migrations recorded as applied.")

    lines.append("")

    if report.pending:
        lines.append("Pending:")
        lines.extend(f"  {path.name}" for path in report.pending)
    else:
        lines.append("Pending: none")

    return "\n".join(lines)


async def _connect() -> asyncpg.Connection:
    # Inside the function on purpose: importing this module must not require
    # DATABASE_URL, or `--help`-shaped mistakes fail with a settings error.
    settings = get_settings()

    return await asyncpg.connect(settings.database_url.get_secret_value())


async def _run_apply(path: Path) -> None:
    connection = await _connect()

    try:
        async with connection.transaction():
            message = await apply_migration(connection, path)
    finally:
        await connection.close()

    # Printed only after the transaction commits; announcing success from
    # inside it would report a migration that a failed commit rolled back.
    print(message)


async def _run_status() -> None:
    connection = await _connect()

    try:
        # The ledger prologue writes (the table, and possibly the 001
        # adoption row), so status needs a transaction as much as apply does.
        async with connection.transaction():
            report = await migration_status(connection)
    finally:
        await connection.close()

    print(render_status(report))

    if report.has_mismatch:
        raise SystemExit(
            "\nAn applied migration no longer matches its recorded checksum."
        )


async def main(argv: list[str]) -> None:
    if len(argv) != 1:
        raise SystemExit(USAGE)

    if argv[0] == "--status":
        await _run_status()
        return

    path = Path(argv[0])

    if not path.is_file():
        raise SystemExit(f"Migration file not found: {path}")

    await _run_apply(path)


if __name__ == "__main__":
    try:
        asyncio.run(main(sys.argv[1:]))
    except MigrationError as error:
        raise SystemExit(f"error: {error}") from error
