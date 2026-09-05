"""Migration 002 applied by the real runner, over rows that predate it.

`test_migration_lint.py` reads 002 as text and `test_apply_migration.py` drives
the runner against fakes. Neither can answer the questions this migration
actually raises, because both of them are arguments about a file rather than
observations of a database:

  * a backfill is only correct against rows that were already there, and the
    interesting rows are the awkward ones -- an empty-string description a
    sloppy `coalesce` would turn into NULL, a `completed_at` a rewrite would
    drop, a title carrying an apostrophe and a multibyte character;
  * a composite foreign key over `(workspace_id, team_id)` and a pair of
    single-column ones are indistinguishable in every catalog summary that
    does not read the column ORDER, and behave identically until someone
    files an issue against a team in another tenant;
  * `ON DELETE RESTRICT` and `ON DELETE CASCADE` are one word apart in the
    file and are the difference between a refused `DELETE FROM teams` and a
    silent loss of every issue in the product.

So this file builds the production genealogy rather than the end state: 001's
schema, populated, with an empty ledger -- which is what `_adopt_initial_migration`
exists for -- and then applies 002 through `scripts.apply_migration` exactly as
an operator would. Everything afterwards is asked of the server.

Two disciplines are borrowed from `test_issue_pagination_db.py`. Ids come from
a suffix scheme whose hex order is its decimal order, so Python and PostgreSQL
sort the seed alike; and expected values are computed in Python from the seed
literals, never by re-running the query under test.

Marked `db`: deselected by default, skipped when Docker is unreachable.
"""

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from operator import attrgetter
from pathlib import Path
from uuid import UUID

import asyncpg
import pytest

from scripts.apply_migration import (
    CHECKSUM_OK,
    apply_migration,
    compute_checksum,
    migration_status,
    read_migration,
)


pytestmark = pytest.mark.db

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"
MIGRATION_001 = MIGRATIONS_DIR / "001_issues.sql"
MIGRATION_002 = MIGRATIONS_DIR / "002_tenancy.sql"

# Written as literals rather than read back out of the database. 002 names
# these two rows in the file precisely so that a test can assert against a
# constant instead of querying for the value it is about to check.
BOOTSTRAP_WORKSPACE_ID = UUID("00000000-0000-7000-8000-000000000001")
BOOTSTRAP_TEAM_ID = UUID("00000000-0000-7000-8000-000000000002")

# A second tenant, created by the tests rather than by the migration. Its whole
# purpose is to make "team from another workspace" expressible at all.
OTHER_WORKSPACE_ID = UUID("00000000-0000-7000-8000-0000000000b1")
OTHER_TEAM_ID = UUID("00000000-0000-7000-8000-0000000000b2")

EMPTY_TEAM_ID = UUID("00000000-0000-7000-8000-0000000000c1")
EMPTY_WORKSPACE_ID = UUID("00000000-0000-7000-8000-0000000000c2")
REJECTED_WORKSPACE_ID = UUID("00000000-0000-7000-8000-0000000000d1")

KEYSET_INDEX = "issues_workspace_created_at_id_idx"
TEAM_LOOKUP_INDEX = "issues_workspace_team_idx"
UNSCOPED_KEYSET_INDEX = "issues_created_at_id_idx"

SLUG_REGEX = "^[a-z0-9]([a-z0-9-]*[a-z0-9])?$"

# The whole rendered CHECK, not just the regex inside it. A substring test for
# SLUG_REGEX alone passes `CHECK (slug ~ '...' OR slug = '')`, which still
# rejects 'Vector' and so satisfies the behavioural half of that test too --
# the constraint would read as intact in both halves while admitting an empty
# slug. Compared after `_normalize_sql`, exactly as PRIORITY_CHECK_DEFINITION
# is below, so the two CHECK assertions in this file are one technique rather
# than two of differing strength.
# The literal gains a `::text` cast on the way out of the catalog; `slug`
# itself does not, because it is already text. Taken from a live PostgreSQL 18
# rather than guessed -- the first version of this constant assumed a
# `(slug)::text` cast that a `character varying` column would have had and this
# one does not.
SLUG_CHECK_DEFINITION = f"CHECK ((slug ~ '{SLUG_REGEX}'::text))"

# PostgreSQL rewrites BETWEEN, so 001's `CHECK (priority BETWEEN 0 AND 4)`
# comes back out of the catalog in this form. Compared after normalization, so
# the spacing is documentation rather than load-bearing.
PRIORITY_CHECK_DEFINITION = "CHECK (((priority >= 0) AND (priority <= 4)))"

# The single-byte codes pg_constraint stores for referential actions. Pinned as
# bytes rather than read through pg_get_constraintdef: the byte is what the
# executor consults, and pinning it is what stops RESTRICT drifting into
# CASCADE behind a rendering that still reads plausibly.
NO_ACTION = "a"
RESTRICT = "r"
CASCADE = "c"
SET_NULL = "n"
SET_DEFAULT = "d"

# Every action byte that is not RESTRICT, so a failure can name what the
# schema chose instead rather than printing a bare letter.
NOT_RESTRICT = frozenset({NO_ACTION, CASCADE, SET_NULL, SET_DEFAULT})

# `confmatchtype`, in the same spirit: 's' is MATCH SIMPLE, 'f' MATCH FULL,
# 'p' MATCH PARTIAL (accepted by the catalog, unimplemented by PostgreSQL).
# MATCH SIMPLE is not merely the default here, it is the semantics
# migrations/002_tenancy.sql reasons about -- it skips the check entirely for
# any row with a NULL referencing column, which is the whole reason both
# tenancy columns are NOT NULL.
MATCH_SIMPLE = "s"

ISSUE_COLUMNS_FROM_001 = frozenset(
    {
        "id",
        "title",
        "description",
        "priority",
        "completed_at",
        "created_at",
        "updated_at",
    }
)

# The tenancy tables fingerprinted the way scripts/apply_migration.py:92-121
# fingerprints the table 001 creates: column -> (type as information_schema
# spells it, nullable, normalized default). A name set is not a schema. A
# column called `slug` that is nullable admits unlimited slugless workspaces
# past a UNIQUE that permits many NULLs and a CHECK that is NULL-not-false; a
# `teams.workspace_id` that is nullable switches teams_workspace_fk off for
# that row under MATCH SIMPLE; a `teams.id` without its default makes every
# insert that omits an id fail at runtime. None of those change the column
# names, and all of them change the schema.
#
# `None` for the default means the column must have no default *at all* -- a
# default 002 does not create is drift like any other, which is the same rule
# INITIAL_DEFAULTS states at scripts/apply_migration.py:107-112.
WORKSPACE_COLUMNS = {
    "id": ("uuid", False, "uuidv7()"),
    "slug": ("text", False, None),
    "name": ("text", False, None),
    "created_at": ("timestamp with time zone", False, "now()"),
}

TEAM_COLUMNS = {
    "id": ("uuid", False, "uuidv7()"),
    "workspace_id": ("uuid", False, None),
    "name": ("text", False, None),
    "created_at": ("timestamp with time zone", False, "now()"),
}

# The two columns 002 adds to issues, fingerprinted the same way.
#
# The `None` defaults here are the load-bearing entries in this whole file.
# migrations/002_tenancy.sql:102-109 argues at length against giving these
# columns a DEFAULT -- "the default outlives the migration and every later
# insert that forgets a workspace silently lands in the bootstrap tenant
# instead of being rejected" -- and until this fingerprint existed, nothing
# tested the claim. A DEFAULT here would convert the loud NotNullViolationError
# that IssueRepository.create currently raises against the post-002 schema into
# a silent cross-tenant write, which is the failure this phase exists to make
# impossible.
TENANCY_COLUMNS_ON_ISSUES = {
    "workspace_id": ("uuid", False, None),
    "team_id": ("uuid", False, None),
}

# The seven columns 001 owns, in the order `SeedRow.as_tuple` writes them.
SNAPSHOT_SQL = """
SELECT id, title, description, priority, completed_at, created_at, updated_at
FROM issues
ORDER BY id
"""

INSERT_ISSUE_SQL = """
    INSERT INTO issues (
        id,
        title,
        description,
        priority,
        completed_at,
        created_at,
        updated_at
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7)
"""

COLUMNS_SQL = """
SELECT column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_schema = 'public' AND table_name = $1
"""

# PostgreSQL renders a default back with casts and parens that say nothing
# about what it evaluates to: `uuidv7()` comes back as written, but a text
# default arrives as `'x'::text` and a numeric one as `(0)::smallint`. Folded
# the same way scripts/apply_migration.py:350-376 folds them, and for the same
# reason -- this is normalized-catalog comparison, not semantic proof about
# arbitrary expressions, and anything it cannot reduce fails to match rather
# than passing by accident.
DEFAULT_CAST = re.compile(r"::[a-z0-9_ .\"]+")

# Constraints by structure. Three parts of this are load-bearing.
#
# `contype IN ('p','u','f','c')` is not decoration: PostgreSQL 18 catalogues
# NOT NULL constraints too, as `contype = 'n'` (see the same note at
# scripts/apply_migration.py:168-170), so without the filter every NOT NULL
# column arrives here as an unexpected check constraint.
#
# `conkey` and `confkey` are unnested WITH ORDINALITY and resolved to column
# names *in order*, because the order is most of the assertion:
# `(team_id, workspace_id) -> (id, workspace_id)` is a different constraint
# that any set-wise comparison would wave straight through.
#
# `confdeltype`/`confupdtype` are cast to text because they are `"char"`, and
# non-foreign-key rows carry a zero byte there rather than a letter.
CONSTRAINTS_SQL = """
SELECT
    con.conname,
    con.contype::text AS contype,
    con.confdeltype::text AS delete_action,
    con.confupdtype::text AS update_action,
    con.confmatchtype::text AS match_type,
    con.condeferrable AS deferrable,
    con.condeferred AS deferred,
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
    ) AS foreign_columns
FROM pg_constraint con
LEFT JOIN pg_class referenced ON referenced.oid = con.confrelid
WHERE con.conrelid = $1::text::regclass
    AND con.contype IN ('p', 'u', 'f', 'c')
"""

# The same shape as ISSUES_INDEX_SQL at scripts/apply_migration.py:149-166,
# parameterised by index name. Structure rather than `pg_indexes.indexdef`:
# that column is a *rendering*, and a test that greps it asserts as much about
# the renderer as about the index. `indkey` is cast so unnest accepts it;
# `indoption` is left alone and subscripted 0-based, which is how PostgreSQL
# numbers it.
INDEX_SQL = """
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
    AND pg_class.relname::text = $1
ORDER BY key_column.ord
"""

SEED_DAY = (2026, 1, 1)

# `updated_at` is deliberately unrelated to `created_at`, so a backfill that
# stamped it -- an UPDATE against a table someone later put a touch trigger on
# -- shows up as a changed value rather than an accidentally identical one.
UPDATED_BASE = datetime(2026, 2, 2, 8, 0, 0, tzinfo=timezone.utc)


def _at(hour: int, minute: int, second: int, microsecond: int = 0) -> datetime:
    return datetime(*SEED_DAY, hour, minute, second, microsecond, tzinfo=timezone.utc)


# Two rows carry this exact value, microseconds included. A backfill has no
# business caring, which is the point: it is one more way for a rewrite of the
# table to lose precision without losing a row.
TIED = _at(12, 5, 0, 123456)


def _issue_id(suffix: int) -> UUID:
    """A UUID whose sort position follows `suffix`.

    PostgreSQL compares `uuid` bytewise over all sixteen bytes rather than as
    text, so `ORDER BY id` and Python's `sorted` need not agree in general.
    They agree by construction here: every id shares the same leading ten
    bytes and the trailing six are twelve decimal digits, whose hex ordering
    is their decimal ordering. That matters because the preservation snapshot
    is ordered by id on the server and compared against a list sorted in
    Python.
    """
    return UUID(f"a1b2c3d4-0000-4000-8000-{suffix:012d}")


@dataclass(frozen=True, slots=True)
class SeedRow:
    id: UUID
    title: str
    description: str | None
    priority: int
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime

    def as_tuple(self) -> tuple:
        """The row as SNAPSHOT_SQL selects it, column for column."""
        return (
            self.id,
            self.title,
            self.description,
            self.priority,
            self.completed_at,
            self.created_at,
            self.updated_at,
        )


def _row(
    suffix: int,
    created_at: datetime,
    *,
    priority: int,
    description: str | None = None,
    completed_at: datetime | None = None,
    title: str | None = None,
) -> SeedRow:
    return SeedRow(
        id=_issue_id(suffix),
        title=title if title is not None else f"issue {suffix:03d}",
        description=description,
        priority=priority,
        completed_at=completed_at,
        created_at=created_at,
        updated_at=UPDATED_BASE + timedelta(seconds=suffix),
    )


# Written in insertion order, which is a deliberate scramble: it is neither id
# order, nor its reverse, nor `(created_at DESC, id DESC)`, nor that reversed.
# Nothing about heap order can stand in for the `ORDER BY id` the snapshot uses.
#
# The variation is chosen for what a careless backfill breaks rather than for
# coverage of the type system:
#
#   * suffix 70 holds `description = ''`. The empty string is NOT NULL and
#     equal to nothing else in the table, so a rewrite written with
#     `coalesce(description, '')` or its mirror image shows here and in no
#     other row.
#   * suffixes 20 and 90 hold `description IS NULL`, the other side of that.
#   * exactly one row (30) has `completed_at` set, so a backfill that dropped
#     the column would leave the other eight looking untouched.
#   * priorities span 0-4, the full range 001's `issues_priority_range`
#     admits, so the check surviving 002 is exercised rather than assumed.
#   * suffix 60's title carries an apostrophe and two multibyte characters,
#     which is what a migration that rebuilt the table through a text
#     round trip would mangle.
SEED = (
    _row(60, _at(12, 4, 0), priority=0, title="Café — impossible d'ouvrir"),
    _row(30, _at(12, 9, 0, 500000), priority=2, completed_at=_at(13, 0, 0)),
    _row(90, _at(12, 6, 0), priority=4, description=None),
    _row(10, _at(12, 8, 0), priority=3, description="second newest"),
    _row(80, TIED, priority=2, description="tied, higher id"),
    _row(40, _at(12, 2, 0, 999999), priority=1, description="microsecond edge"),
    _row(70, TIED, priority=1, description=""),
    _row(20, _at(12, 1, 0), priority=0, description=None),
    _row(50, _at(12, 3, 0), priority=4, description="oldest of the middle band"),
)

# The expectation, computed from the seed literals by sorting them, never by
# asking the database what it holds.
SEED_BY_ID = sorted(SEED, key=attrgetter("id"))
SEED_TUPLES = [row.as_tuple() for row in SEED_BY_ID]

# The dataset the EXPLAIN tests plan against. Split evenly between two
# workspaces so that `workspace_id = $1` is a real restriction rather than a
# predicate matching every row, which the planner would treat quite differently.
BULK_ROWS = 4000
BULK_BASE = datetime(2026, 3, 1, 0, 0, 0, tzinfo=timezone.utc)

# Even `n` lands in the bootstrap workspace, so the 1000th newest row there is
# `n = BULK_ROWS - 2 * 999`. Derived rather than fetched with OFFSET: the
# generated rows are deterministic, so the cursor can be computed the same way
# every other expectation in this file is.
CURSOR_N = BULK_ROWS - 2 * 999
CURSOR_CREATED_AT = BULK_BASE + timedelta(seconds=CURSOR_N)
CURSOR_ID = UUID(f"a1b2c3d4-0000-4000-9000-{CURSOR_N:012d}")

PAGE_SIZE = 51

# The scaling dataset for the tenant-isolation test below. Our tenant holds a
# fixed number of rows in both runs; only the NEIGHBOUR's row count changes,
# which is what makes the measurement a statement about tenant isolation
# rather than about table size.
TENANT_ROWS = 500
SMALL_NEIGHBOUR = 500
LARGE_NEIGHBOUR = 20000

# How many extra buffers our tenant's first page may read when the neighbour
# grows 40x. Measured on PostgreSQL 18: the correctly ordered index reads 4
# then 5 (the single extra page is the btree gaining a level), while an index
# ordered (created_at DESC, id DESC, workspace_id) reads 10 then 35. The
# threshold sits far above the former and far below the latter deliberately --
# tight enough to fail on a walking scan, loose enough not to fail on a page
# layout or a btree level.
NEIGHBOUR_GROWTH_ALLOWANCE = 8

# Rows for our tenant, spaced 40 seconds apart, and the neighbour's, spaced one
# second apart, over the same window. The interleaving is the point: every
# window of the global (created_at DESC, id DESC) ordering holds roughly forty
# neighbour rows for each of ours, so an index that cannot restrict to a tenant
# has to walk past all of them. Our rows are inserted first and so sit
# physically together in the heap, which keeps heap access identical between
# the two index shapes and leaves the buffer difference attributable to the
# index walk alone.
TENANT_BULK_SQL = """
INSERT INTO issues (
    id, workspace_id, team_id, title, priority, created_at, updated_at
)
SELECT
    ('a1b2c3d4-0000-4000-9000-' || lpad(n::text, 12, '0'))::UUID,
    $1::UUID,
    $2::UUID,
    'tenant issue ' || n,
    n % 5,
    $3::TIMESTAMPTZ + (n * 40) * INTERVAL '1 second',
    $3::TIMESTAMPTZ + (n * 40) * INTERVAL '1 second'
FROM generate_series(1, $4::INT) AS n
"""

NEIGHBOUR_BULK_SQL = """
INSERT INTO issues (
    id, workspace_id, team_id, title, priority, created_at, updated_at
)
SELECT
    ('a1b2c3d4-0000-4000-a000-' || lpad(n::text, 12, '0'))::UUID,
    $1::UUID,
    $2::UUID,
    'neighbour issue ' || n,
    n % 5,
    $3::TIMESTAMPTZ + n * INTERVAL '1 second',
    $3::TIMESTAMPTZ + n * INTERVAL '1 second'
FROM generate_series(1, $4::INT) AS n
"""

BULK_INSERT_SQL = """
INSERT INTO issues (
    id, workspace_id, team_id, title, priority, created_at, updated_at
)
SELECT
    ('a1b2c3d4-0000-4000-9000-' || lpad(n::text, 12, '0'))::UUID,
    CASE WHEN n % 2 = 0 THEN $1::UUID ELSE $3::UUID END,
    CASE WHEN n % 2 = 0 THEN $2::UUID ELSE $4::UUID END,
    'bulk issue ' || n,
    n % 5,
    $5::TIMESTAMPTZ + n * INTERVAL '1 second',
    $5::TIMESTAMPTZ + n * INTERVAL '1 second'
FROM generate_series(1, $6::INT) AS n
"""

FIRST_PAGE_SQL = """
SELECT id, title, description, priority, completed_at, created_at, updated_at
FROM issues
WHERE workspace_id = $1
ORDER BY created_at DESC, id DESC
LIMIT 51
"""

CURSOR_PAGE_SQL = """
SELECT id, title, description, priority, completed_at, created_at, updated_at
FROM issues
WHERE workspace_id = $1 AND (created_at, id) < ($2, $3)
ORDER BY created_at DESC, id DESC
LIMIT 51
"""


def _normalize_sql(text: str) -> str:
    """Fold case and whitespace, which is all `pg_get_constraintdef` varies."""
    return " ".join(text.split()).lower()


async def _constraints(connection, table: str) -> dict[str, dict]:
    rows = await connection.fetch(CONSTRAINTS_SQL, table)

    return {row["conname"]: dict(row) for row in rows}


async def _index_structure(connection, index: str) -> list[dict]:
    return [dict(row) for row in await connection.fetch(INDEX_SQL, index)]


async def _column_names(connection, table: str) -> set[str]:
    rows = await connection.fetch(COLUMNS_SQL, table)

    return {row["column_name"] for row in rows}


def _normalize_default(expression: str | None) -> str | None:
    """Reduce a catalogued default to a form worth comparing, or None.

    Deliberately the same reduction as `_normalize_default` at
    scripts/apply_migration.py:350-376: fold case and whitespace, drop
    `public.` qualification and casts, strip redundant parens, and treat
    CURRENT_TIMESTAMP as now() because PostgreSQL stores that one as written
    rather than folding it. Exact for the two defaults 002 declares; for
    anything else it is conservative in the safe direction, since an
    expression it cannot reduce simply fails to match.
    """
    if expression is None:
        return None

    reduced = "".join(expression.split()).lower()

    if not reduced:
        return None

    reduced = reduced.replace("public.", "")
    reduced = DEFAULT_CAST.sub("", reduced)

    while reduced.startswith("(") and reduced.endswith(")"):
        reduced = reduced[1:-1]

    return "now()" if reduced == "current_timestamp" else reduced


async def _column_fingerprint(
    connection,
    table: str,
) -> dict[str, tuple[str, bool, str | None]]:
    """`column -> (type, nullable, normalized default)` for one table."""
    rows = await connection.fetch(COLUMNS_SQL, table)

    return {
        row["column_name"]: (
            row["data_type"],
            row["is_nullable"] == "YES",
            _normalize_default(row["column_default"]),
        )
        for row in rows
    }


async def _snapshot(connection) -> list[tuple]:
    return [tuple(row) for row in await connection.fetch(SNAPSHOT_SQL)]


def _plan_nodes(node: dict):
    """Every node of an EXPLAIN plan tree; children hang off `Plans`."""
    yield node

    for child in node.get("Plans", ()):
        yield from _plan_nodes(child)


async def _plan(executor, sql: str, *args) -> dict:
    rendered = await executor.fetchval(
        f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql}",
        *args,
    )

    return json.loads(rendered)[0]["Plan"]


def _buffers(plan: dict) -> int:
    """Shared blocks touched by the whole plan.

    Read off the root node, where EXPLAIN's buffer counts are cumulative over
    the subtree. Hit and read are summed because the split between them is a
    fact about the buffer cache's warmth, not about the query.
    """
    return plan.get("Shared Hit Blocks", 0) + plan.get("Shared Read Blocks", 0)


def _assert_index_serves_the_order(plan: dict, *, label: str) -> None:
    """The plan reaches the rows through KEYSET_INDEX and sorts nothing.

    Both halves matter. An index scan with a Sort above it means the index was
    used for the *predicate* and not for the *order*, which is the state the
    keyset walk cannot afford: it would mean the server materialises and sorts
    the whole workspace before applying the LIMIT.

    `Incremental Sort` is caught alongside `Sort` -- it is a partial sort by
    another name, and a plan that needs one is a plan whose index does not
    deliver the full ordering.

    What this helper does NOT catch, established by mutation rather than by
    reading it: an index on `(created_at DESC, id DESC, workspace_id)`. That
    index still delivers the ordering, so it still produces an Index Scan on
    this name with no Sort above it, and every assertion here passes. It
    simply cannot restrict to a tenant, so it walks the global ordering and
    discards other workspaces' rows as it goes.

    Nothing in the plan JSON names that difference. On PostgreSQL 18 the
    tenant equality is reported under `Index Cond` for both column orders --
    a non-leading equality on a column the index contains is still an index
    qual, evaluated inside the access method -- so neither `Filter`,
    `Rows Removed by Filter`, `Rows Removed by Index Recheck`,
    `Index Searches` nor `Actual Rows` differs at all. The two are separated
    only by how much work they do, which is what
    test_a_tenant_scoped_page_does_not_pay_for_another_tenants_rows measures,
    and by the catalog, which remains the primary guard on column order.
    """
    nodes = list(_plan_nodes(plan))

    scans = [
        node
        for node in nodes
        if node.get("Index Name") == KEYSET_INDEX
        and node["Node Type"] in {"Index Scan", "Index Only Scan"}
    ]

    assert scans, (
        f"{label}: nothing in the plan scans {KEYSET_INDEX}; node types were "
        f"{[node['Node Type'] for node in nodes]}"
    )

    sorts = [node["Node Type"] for node in nodes if node["Node Type"].endswith("Sort")]

    assert not sorts, (
        f"{label}: the plan contains {sorts}, so {KEYSET_INDEX} is not "
        "delivering the (created_at DESC, id DESC) order the keyset relies on"
    )

    # The tenant predicate has to reach the index. This does not distinguish a
    # leading `workspace_id` from a trailing one -- see the docstring -- but it
    # does catch the predicate falling out of the index entirely, which is what
    # an index over `(created_at DESC, id DESC)` alone would produce.
    index_cond = scans[0].get("Index Cond", "")

    assert "workspace_id" in index_cond, (
        f"{label}: the tenant predicate is not an index condition "
        f"(Index Cond: {index_cond!r}), so the scan is not restricted by "
        "workspace at the index level at all"
    )

    # `Filter` is absent altogether on a correct plan, and `Rows Removed by
    # Filter` appears only when ANALYZE ran and a node discarded something, so
    # a missing key is zero rather than a failure to look.
    discarded = [
        (node["Node Type"], node.get("Filter"), node["Rows Removed by Filter"])
        for node in nodes
        if node.get("Rows Removed by Filter", 0)
    ]

    assert not discarded, (
        f"{label}: the plan reads rows only to throw them away: {discarded}. "
        "A tenant-scoped page must be positioned by the index, not filtered "
        "after the fact"
    )


@dataclass(frozen=True, slots=True)
class Applied:
    connection: asyncpg.Connection
    before: list[tuple]
    message: str


@dataclass(frozen=True, slots=True)
class Planner:
    free: asyncpg.Connection
    forced: asyncpg.Pool


@pytest.fixture
async def applied(postgres_dsn):
    """001's schema, populated, then 002 applied through the real runner.

    Every table is dropped first, and `teams` and `workspaces` go with them.
    The container is shared for the whole session, so leaving either behind
    would make the *next* run of this file fail inside 002's
    `CREATE TABLE workspaces` -- a failure with nothing to do with the
    migration. One `DROP TABLE` naming all three resolves the foreign keys
    between them itself.

    The ledger is dropped too, which is what puts the database in the state
    the runner's adoption path exists for: a schema 001 has reached and the
    runner never has. That is the production genealogy, so 002 is applied over
    it rather than over a ledger some test wrote by hand.
    """
    connection = await asyncpg.connect(postgres_dsn)

    try:
        await connection.execute("DROP TABLE IF EXISTS issues, teams, workspaces")
        await connection.execute("DROP TABLE IF EXISTS schema_migrations")
        await connection.execute(read_migration(MIGRATION_001))
        await connection.executemany(
            INSERT_ISSUE_SQL,
            [row.as_tuple() for row in SEED],
        )

        before = await _snapshot(connection)

        async with connection.transaction():
            message = await apply_migration(
                connection,
                MIGRATION_002,
                migrations_dir=MIGRATIONS_DIR,
            )

        yield Applied(connection=connection, before=before, message=message)
    finally:
        await connection.close()


@pytest.fixture
def connection(applied: Applied) -> asyncpg.Connection:
    return applied.connection


@pytest.fixture
async def two_tenants(connection) -> asyncpg.Connection:
    """A second workspace with a team of its own.

    002 creates one tenant, so "a team belonging to another workspace" is not
    expressible against what it leaves behind. These two rows are what make
    the composite foreign key testable at all.
    """
    await connection.execute(
        "INSERT INTO workspaces (id, slug, name) VALUES ($1, $2, $3)",
        OTHER_WORKSPACE_ID,
        "beta",
        "Beta",
    )
    await connection.execute(
        "INSERT INTO teams (id, workspace_id, name) VALUES ($1, $2, $3)",
        OTHER_TEAM_ID,
        OTHER_WORKSPACE_ID,
        "Beta Core",
    )

    return connection


@pytest.fixture
async def planner(two_tenants, postgres_dsn):
    """A few thousand issues across two workspaces, analyzed.

    Size and statistics are both deliberate. On the nine seeded rows the
    planner has no cardinality to work with and picks whatever its defaults
    favour, which is how a plan assertion ends up describing the fixture
    instead of the schema (see the comment at
    tests/test_issue_pagination_db.py:333-351 for the version of that mistake
    this suite already shipped once). Two workspaces make `workspace_id = $1`
    a restriction the planner can cost rather than a predicate matching
    everything.
    """
    await two_tenants.execute(
        BULK_INSERT_SQL,
        BOOTSTRAP_WORKSPACE_ID,
        BOOTSTRAP_TEAM_ID,
        OTHER_WORKSPACE_ID,
        OTHER_TEAM_ID,
        BULK_BASE,
        BULK_ROWS,
    )
    await two_tenants.execute("ANALYZE issues")

    pool = await asyncpg.create_pool(
        dsn=postgres_dsn,
        min_size=1,
        max_size=2,
        server_settings={"enable_seqscan": "off"},
    )

    try:
        yield Planner(free=two_tenants, forced=pool)
    finally:
        await pool.close()


# --------------------------------------------------------------------------
# A. Data preservation
# --------------------------------------------------------------------------


async def test_002_preserves_every_pre_existing_issue_column_for_column(applied):
    """The nine seeded rows, all seven of their original columns, unchanged.

    Compared as lists rather than sets, and never as a digest. A set absorbs a
    duplicated row silently -- exactly what a migration that rebuilt the table
    could produce -- and a checksum tells you *that* something changed while
    refusing to say which column of which row, which is the entire value of
    this test on the day it fails.

    The before-snapshot is checked against the seed literals as well as
    against the after-snapshot. Without that, `before == after` still passes
    when the fixture seeded nothing at all and both are empty.
    """
    after = await _snapshot(applied.connection)

    assert applied.before == SEED_TUPLES, (
        "the fixture did not put the seed rows in the table, so the "
        "preservation comparison below would prove nothing"
    )

    assert len(after) == len(SEED)
    assert [row[0] for row in after] == [row.id for row in SEED_BY_ID]
    assert after == applied.before


# --------------------------------------------------------------------------
# B. Tenancy columns populated
# --------------------------------------------------------------------------


async def test_the_backfill_left_no_null_in_either_tenancy_column(connection):
    ownerless = await connection.fetchval(
        "SELECT count(*) FROM issues WHERE workspace_id IS NULL"
    )
    teamless = await connection.fetchval(
        "SELECT count(*) FROM issues WHERE team_id IS NULL"
    )

    assert ownerless == 0
    assert teamless == 0


async def test_every_pre_existing_issue_landed_in_the_one_bootstrap_tenant(connection):
    """DISTINCT rather than a per-row equality check.

    A partial backfill -- some rows in the bootstrap tenant, some elsewhere --
    fails here as a second row in the result, and the failure names both pairs
    instead of naming one arbitrary row that happened to be checked first.
    """
    pairs = await connection.fetch("SELECT DISTINCT workspace_id, team_id FROM issues")

    assert [(row["workspace_id"], row["team_id"]) for row in pairs] == [
        (BOOTSTRAP_WORKSPACE_ID, BOOTSTRAP_TEAM_ID)
    ]


async def test_both_tenancy_columns_are_declared_not_null(connection):
    """Asserted separately from the data, because they are separate claims.

    A migration can backfill every row and forget the `SET NOT NULL`, and the
    resulting schema passes every data check above while admitting an
    ownerless issue on the next insert. Worse than ownerless: 002's own
    comment records that a composite FK is MATCH SIMPLE, so a NULL in either
    column switches the tenant-pair check off entirely for that row.
    """
    rows = await connection.fetch(COLUMNS_SQL, "issues")
    nullability = {row["column_name"]: row["is_nullable"] for row in rows}

    assert nullability["workspace_id"] == "NO"
    assert nullability["team_id"] == "NO"


# --------------------------------------------------------------------------
# C. Bootstrap rows
# --------------------------------------------------------------------------


async def test_002_seeds_exactly_one_workspace_and_one_team(connection):
    workspaces = await connection.fetch("SELECT id, slug, name FROM workspaces")
    teams = await connection.fetch("SELECT id, workspace_id, name FROM teams")

    assert [tuple(row) for row in workspaces] == [
        (BOOTSTRAP_WORKSPACE_ID, "vector", "Vector")
    ]
    assert [tuple(row) for row in teams] == [
        (BOOTSTRAP_TEAM_ID, BOOTSTRAP_WORKSPACE_ID, "Core")
    ]


# --------------------------------------------------------------------------
# D. Column sets
# --------------------------------------------------------------------------


async def test_the_tenancy_tables_carry_exactly_the_approved_columns(connection):
    """Dict equality in both directions: name, type, nullability and default.

    A name set was what this asserted first, and a name set is not a schema.
    Every one of these passes a name-set comparison unchanged:

      * `workspaces.slug` nullable -- UNIQUE permits many NULLs and
        `slug ~ '...'` is NULL-rather-than-false, so unlimited slugless
        workspaces insert cleanly;
      * `teams.workspace_id` nullable -- MATCH SIMPLE skips teams_workspace_fk
        for any row with a NULL, so a team belongs to no workspace;
      * `teams.id` losing `DEFAULT uuidv7()` -- every insert that omits an id
        fails at runtime instead of at migration time;
      * `workspaces.created_at` losing NOT NULL and its default.

    A subset check would additionally pass a `teams.slug`, a `teams.key` or an
    `updated_at` on either table, all three considered and deliberately left
    out. Equality both ways is what makes an unasked-for column a failure.
    """
    assert await _column_fingerprint(connection, "workspaces") == WORKSPACE_COLUMNS
    assert await _column_fingerprint(connection, "teams") == TEAM_COLUMNS


async def test_002_adds_exactly_workspace_id_and_team_id_to_issues(connection):
    assert await _column_names(connection, "issues") == ISSUE_COLUMNS_FROM_001 | {
        "workspace_id",
        "team_id",
    }


async def test_the_tenancy_columns_on_issues_are_not_null_and_carry_no_default(
    connection,
):
    """The single assertion this file most needed and did not have.

    `NOT NULL` was already pinned. The absence of a DEFAULT was not, and that
    is the half that matters more, because a DEFAULT here fails silently in
    the one direction tenancy cannot tolerate.

    migrations/002_tenancy.sql:102-109 sets out the argument: a default
    outlives the migration, so every later insert that forgets a workspace
    lands in the bootstrap tenant rather than being rejected. Concretely,
    IssueRepository.create names only title/description/priority
    (app/repositories/issues.py:51-71); against the post-002 schema it raises
    NotNullViolationError, loudly, which is exactly the signal that the
    repository has not yet been made tenant-aware. Add
    `DEFAULT '...0001'::UUID` and that signal becomes a successful insert into
    the bootstrap workspace -- a cross-tenant write that no test, no type and
    no constraint would report.

    Asserted as a whole-dict comparison rather than two lookups so that a
    third tenancy column arriving on issues is a failure too.
    """
    fingerprint = await _column_fingerprint(connection, "issues")

    assert {
        name: value
        for name, value in fingerprint.items()
        if name not in ISSUE_COLUMNS_FROM_001
    } == TENANCY_COLUMNS_ON_ISSUES


# --------------------------------------------------------------------------
# E. Constraints
# --------------------------------------------------------------------------


async def test_the_issues_team_foreign_key_is_composite_ordered_and_restricting(
    connection,
):
    """The constraint this whole migration exists to declare.

    Every clause below is a separate way for the schema to look right and
    behave wrongly:

      * the local columns in order, because `(team_id, workspace_id)` is a
        different constraint that an unordered comparison accepts;
      * the referenced columns in order, for the same reason on the other side;
      * the referenced table, because a FK onto `workspaces` would satisfy a
        name-only check while enforcing nothing about teams;
      * the two action bytes, because RESTRICT and CASCADE are one word apart
        in the migration file and the difference is whether
        `DELETE FROM teams` refuses or destroys every issue in the product;
      * `condeferrable`, because a deferred constraint is not checked until
        COMMIT. Every test in this file runs in autocommit, so its implicit
        transaction commits per statement and a DEFERRABLE INITIALLY DEFERRED
        version of this FK passes all of them -- while a service that opens an
        explicit transaction, which IssueService.create does
        (app/services/issues.py:59-68), could insert a cross-tenant issue and
        read it back inside that transaction;
      * `confmatchtype`, because MATCH SIMPLE is what the migration's own
        reasoning rests on: it skips the check for any row with a NULL
        referencing column, which is why both columns are NOT NULL.
    """
    constraint = (await _constraints(connection, "public.issues"))["issues_team_fk"]

    assert constraint["contype"] == "f"
    assert constraint["local_columns"] == ["workspace_id", "team_id"]
    assert constraint["referenced_table"] == "teams"
    assert constraint["foreign_columns"] == ["workspace_id", "id"]
    assert constraint["delete_action"] == RESTRICT
    assert constraint["update_action"] == RESTRICT
    assert constraint["match_type"] == MATCH_SIMPLE
    assert constraint["deferrable"] is False
    assert constraint["deferred"] is False

    # Stated so the failure output names what the byte meant rather than
    # leaving a reader to look it up.
    assert constraint["delete_action"] not in NOT_RESTRICT


async def test_the_teams_workspace_foreign_key_restricts_in_both_directions(connection):
    """The same fingerprint one level up, and for the same reasons.

    MATCH SIMPLE matters here too: `teams.workspace_id` is NOT NULL precisely
    so this constraint cannot be skipped for a team that belongs to nobody.
    """
    constraint = (await _constraints(connection, "public.teams"))["teams_workspace_fk"]

    assert constraint["contype"] == "f"
    assert constraint["local_columns"] == ["workspace_id"]
    assert constraint["referenced_table"] == "workspaces"
    assert constraint["foreign_columns"] == ["id"]
    assert constraint["delete_action"] == RESTRICT
    assert constraint["update_action"] == RESTRICT
    assert constraint["match_type"] == MATCH_SIMPLE
    assert constraint["deferrable"] is False
    assert constraint["deferred"] is False


async def test_teams_carries_the_composite_unique_key_the_issues_fk_targets(connection):
    """`UNIQUE (workspace_id, id)` reads as redundant beside the primary key.

    It is not: a foreign key may only reference a UNIQUE-constrained column
    set, so without this pair `issues_team_fk` cannot be declared at all. The
    column order is asserted because the FK names `(workspace_id, id)` in that
    order and a key on `(id, workspace_id)` would not satisfy it.
    """
    constraints = await _constraints(connection, "public.teams")
    constraint = constraints["teams_workspace_id_key"]

    assert constraint["contype"] == "u"
    assert constraint["local_columns"] == ["workspace_id", "id"]

    assert constraints["teams_pkey"]["contype"] == "p"
    assert constraints["teams_pkey"]["local_columns"] == ["id"]


async def test_the_workspace_slug_unique_constraint_is_present_and_bites(connection):
    constraint = (await _constraints(connection, "public.workspaces"))[
        "workspaces_slug_key"
    ]

    assert constraint["contype"] == "u"
    assert constraint["local_columns"] == ["slug"]

    with pytest.raises(asyncpg.UniqueViolationError) as error:
        await connection.execute(
            "INSERT INTO workspaces (id, slug, name) VALUES ($1, $2, $3)",
            REJECTED_WORKSPACE_ID,
            "vector",
            "Vector Again",
        )

    assert "workspaces_slug_key" in str(error.value)


async def test_the_workspace_slug_check_is_present_and_rejects_an_uppercase_slug(
    connection,
):
    """The catalog entry and the behaviour, because neither implies the other.

    A CHECK can be present and NOT VALID, or present over a regex that admits
    what it was written to exclude. `'Vector'` is the case the constraint
    exists for: UNIQUE on plain TEXT is case-sensitive, so without the check it
    inserts cleanly beside `'vector'` and a client varying capitalisation
    silently addresses a different tenant.
    """
    constraint = (await _constraints(connection, "public.workspaces"))[
        "workspaces_slug_format"
    ]

    assert constraint["contype"] == "c"
    assert _normalize_sql(constraint["definition"]) == _normalize_sql(
        SLUG_CHECK_DEFINITION
    )

    with pytest.raises(asyncpg.CheckViolationError) as error:
        await connection.execute(
            "INSERT INTO workspaces (id, slug, name) VALUES ($1, $2, $3)",
            REJECTED_WORKSPACE_ID,
            "Vector",
            "Vector",
        )

    assert "workspaces_slug_format" in str(error.value)
    assert await connection.fetchval("SELECT count(*) FROM workspaces") == 1


async def test_no_foreign_key_on_issues_points_straight_at_workspaces(connection):
    """The absence of `issues_workspace_fk` is a decision, so it is pinned.

    002 argues the reference is already transitively guaranteed -- the
    composite FK forces a real teams row, and `teams_workspace_fk` forces that
    row's workspace to be real -- and that a second FK would be a second
    deletion path into issues, which is a second place to get ON DELETE wrong.
    An absence nobody asserts is indistinguishable from an omission, and the
    obvious "improvement" is to add one.
    """
    constraints = await _constraints(connection, "public.issues")
    foreign_keys = {
        name: row for name, row in constraints.items() if row["contype"] == "f"
    }

    assert set(foreign_keys) == {"issues_team_fk"}
    assert [row["referenced_table"] for row in foreign_keys.values()] == ["teams"]


async def test_001s_priority_check_and_single_column_primary_key_survived_002(
    connection,
):
    """Two things 002 must not have touched while it was rewriting the table.

    The primary key especially: widening it to `(workspace_id, id)` is a
    plausible-sounding tenancy change that would break `get_by_id`, and it
    would leave every other assertion in this file passing.
    """
    constraints = await _constraints(connection, "public.issues")

    assert _normalize_sql(constraints["issues_priority_range"]["definition"]) == (
        _normalize_sql(PRIORITY_CHECK_DEFINITION)
    )

    assert constraints["issues_pkey"]["contype"] == "p"
    assert constraints["issues_pkey"]["local_columns"] == ["id"]


# --------------------------------------------------------------------------
# F. Indexes
# --------------------------------------------------------------------------


async def test_the_workspace_scoped_keyset_index_has_the_approved_structure(connection):
    """Read out of `pg_index`, not out of `pg_indexes.indexdef`.

    `indexdef` is a rendering, so a test that greps it asserts the renderer as
    much as the index. `workspace_id`'s direction is pinned along with the
    other two even though it cannot affect the plan -- an equality prefix has
    no order to preserve -- because an assertion that silently skips a column
    is weaker than it reads.
    """
    rows = await _index_structure(connection, KEYSET_INDEX)

    assert [(row["column_name"], row["is_desc"]) for row in rows] == [
        ("workspace_id", False),
        ("created_at", True),
        ("id", True),
    ]

    assert rows[0]["is_unique"] is False
    assert rows[0]["is_partial"] is False
    assert rows[0]["has_expressions"] is False


async def test_the_workspace_team_index_has_the_approved_structure(connection):
    """The referencing side of `issues_team_fk`.

    PostgreSQL indexes the referenced side of a foreign key automatically and
    the referencing side never, so without this every `DELETE FROM teams`
    scans issues in full to satisfy the RESTRICT check. The column order is
    the FK's column order because that is the lookup the integrity check
    performs.
    """
    rows = await _index_structure(connection, TEAM_LOOKUP_INDEX)

    assert [(row["column_name"], row["is_desc"]) for row in rows] == [
        ("workspace_id", False),
        ("team_id", False),
    ]

    assert rows[0]["is_unique"] is False
    assert rows[0]["is_partial"] is False
    assert rows[0]["has_expressions"] is False


async def test_the_unscoped_keyset_index_from_001_is_gone(connection):
    """Dropped, not merely superseded.

    Keeping it would cost write amplification on every insert and update in
    exchange for serving a query the application is being changed to stop
    issuing. The same structural query that finds the new indexes returns
    nothing for it.
    """
    assert await _index_structure(connection, UNSCOPED_KEYSET_INDEX) == []


# --------------------------------------------------------------------------
# G. EXPLAIN
# --------------------------------------------------------------------------


async def test_the_scoped_first_page_is_served_by_the_new_index_without_a_sort(planner):
    """A performance-shape canary, and deliberately not a correctness gate.

    Plan selection is a cost estimate. A later PostgreSQL, a different
    `random_page_cost`, or simply a different row count can change what the
    free planner picks here without anything being wrong, so a red result from
    the free half of this test is a prompt to go and look rather than proof of
    a defect. Nothing about the correctness of pagination depends on it: that
    is settled by `test_issue_pagination_db.py` and its adversarial sibling,
    which assert the same answer under four different plans.

    What the run with `enable_seqscan = off` does buy is a claim independent
    of costing: this index *can* serve this query with no sort at all. If the
    index were built on the wrong columns or in the wrong direction, the
    forced run would still have to sort, and no planner setting could hide it.

    The limit of that claim is worth stating, because mutation testing found
    it. "Index Scan on this name, no Sort" is also satisfied by an index on
    `(created_at DESC, id DESC, workspace_id)`, which delivers the ordering
    and cannot restrict to a tenant. This test passes against that index and
    is not the thing that catches it --
    test_a_tenant_scoped_page_does_not_pay_for_another_tenants_rows is, by
    measuring work rather than shape, and the catalog test remains the primary
    guard on column order.
    """
    rows = await planner.free.fetch(FIRST_PAGE_SQL, BOOTSTRAP_WORKSPACE_ID)

    assert len(rows) == PAGE_SIZE, (
        "the first page came back short, so the plan below describes a query "
        "that barely had to do anything"
    )

    _assert_index_serves_the_order(
        await _plan(planner.free, FIRST_PAGE_SQL, BOOTSTRAP_WORKSPACE_ID),
        label="first page, planner free",
    )
    _assert_index_serves_the_order(
        await _plan(planner.forced, FIRST_PAGE_SQL, BOOTSTRAP_WORKSPACE_ID),
        label="first page, sequential scans disabled",
    )


async def test_the_scoped_cursor_page_is_served_by_the_new_index_without_a_sort(
    planner,
):
    """The resume shape, which is the one the index has to answer at speed.

    Same caveats as the first-page test above, both of them: the free run is a
    canary and the forced run is the claim about the index, and neither
    distinguishes a leading `workspace_id` from a trailing one. The row-value
    comparison `(created_at, id) < ($2, $3)` sitting under a leading equality
    on `workspace_id` is exactly what a three-column index in this order can
    walk from a starting point, and what any other index shape would have to
    sort -- but "would have to sort" is the part an index ending in
    `workspace_id` escapes, since it is still ordered by `(created_at, id)`.
    """
    rows = await planner.free.fetch(
        CURSOR_PAGE_SQL,
        BOOTSTRAP_WORKSPACE_ID,
        CURSOR_CREATED_AT,
        CURSOR_ID,
    )

    assert len(rows) == PAGE_SIZE, (
        "the cursor page came back short, so either the cursor is past the "
        "end of the workspace or the bulk dataset is smaller than it looks"
    )

    _assert_index_serves_the_order(
        await _plan(
            planner.free,
            CURSOR_PAGE_SQL,
            BOOTSTRAP_WORKSPACE_ID,
            CURSOR_CREATED_AT,
            CURSOR_ID,
        ),
        label="cursor page, planner free",
    )
    _assert_index_serves_the_order(
        await _plan(
            planner.forced,
            CURSOR_PAGE_SQL,
            BOOTSTRAP_WORKSPACE_ID,
            CURSOR_CREATED_AT,
            CURSOR_ID,
        ),
        label="cursor page, sequential scans disabled",
    )


async def _rebuild_with_neighbour(connection, neighbour_rows: int) -> None:
    """001 + 002 over a fixed tenant and a neighbour of the requested size."""
    await connection.execute("DROP TABLE IF EXISTS issues, teams, workspaces")
    await connection.execute("DROP TABLE IF EXISTS schema_migrations")
    await connection.execute(read_migration(MIGRATION_001))

    async with connection.transaction():
        await apply_migration(
            connection,
            MIGRATION_002,
            migrations_dir=MIGRATIONS_DIR,
        )

    await connection.execute(
        "INSERT INTO workspaces (id, slug, name) VALUES ($1, $2, $3)",
        OTHER_WORKSPACE_ID,
        "beta",
        "Beta",
    )
    await connection.execute(
        "INSERT INTO teams (id, workspace_id, name) VALUES ($1, $2, $3)",
        OTHER_TEAM_ID,
        OTHER_WORKSPACE_ID,
        "Beta Core",
    )
    await connection.execute(
        TENANT_BULK_SQL,
        BOOTSTRAP_WORKSPACE_ID,
        BOOTSTRAP_TEAM_ID,
        BULK_BASE,
        TENANT_ROWS,
    )
    await connection.execute(
        NEIGHBOUR_BULK_SQL,
        OTHER_WORKSPACE_ID,
        OTHER_TEAM_ID,
        BULK_BASE,
        neighbour_rows,
    )
    await connection.execute("ANALYZE issues")


async def _first_page_buffers(connection, *, label: str) -> int:
    """Blocks our tenant's first page touches, with the plan checked first.

    Measuring without checking the plan would silently compare a Seq Scan
    against an Index Scan the moment the planner changed its mind, and report
    the difference as a tenancy defect.
    """
    await connection.fetch(FIRST_PAGE_SQL, BOOTSTRAP_WORKSPACE_ID)

    plan = await _plan(connection, FIRST_PAGE_SQL, BOOTSTRAP_WORKSPACE_ID)
    _assert_index_serves_the_order(plan, label=label)

    return _buffers(plan)


async def test_a_tenant_scoped_page_does_not_pay_for_another_tenants_rows(connection):
    """The assertion that separates a tenant index from a merely ordered one.

    This exists because of a mutation the plan-shape tests above could not
    see. Changing the index to `(created_at DESC, id DESC, workspace_id)`
    leaves it delivering the ORDER BY perfectly, so those tests kept passing:
    same Index Scan, same index name, no Sort. What that index cannot do is
    *position* on a tenant. It walks the global ordering and discards other
    workspaces' rows, at a cost proportional to how much data every other
    tenant holds -- roughly two thousand wasted rows per page on the bulk set,
    and unbounded in production.

    No field of the plan JSON reports that. On PostgreSQL 18 the tenant
    equality shows up under `Index Cond` for both column orders, and
    `Rows Removed by Filter`, `Rows Removed by Index Recheck`,
    `Index Searches` and `Actual Rows` are identical. The two shapes differ
    only in how much they read, so reading is what is measured here.

    The measurement is a scaling one rather than an absolute threshold, which
    is what makes it a claim about tenancy instead of about table size. Our
    tenant holds exactly the same rows in both runs; only the neighbour grows,
    by a factor of forty. A correctly ordered index is indifferent to that --
    measured at 4 blocks then 5, the extra one being the btree gaining a
    level. The misordered index went from 10 to 35.

    Like the two tests above this is a performance-shape canary, not a
    correctness gate, and the catalog test remains the primary guard on column
    order. Its value is that it is a second, behavioural witness, derived from
    the property the column order exists to provide rather than from the
    catalog entry that records it.
    """
    await _rebuild_with_neighbour(connection, SMALL_NEIGHBOUR)
    small = await _first_page_buffers(connection, label="small neighbour")

    await _rebuild_with_neighbour(connection, LARGE_NEIGHBOUR)
    large = await _first_page_buffers(connection, label="large neighbour")

    growth = large - small

    assert growth <= NEIGHBOUR_GROWTH_ALLOWANCE, (
        f"our tenant's first page read {small} blocks beside a "
        f"{SMALL_NEIGHBOUR}-row neighbour and {large} beside a "
        f"{LARGE_NEIGHBOUR}-row one, a growth of {growth}. Our tenant held "
        f"{TENANT_ROWS} rows in both runs, so the page is paying for rows it "
        f"never returns -- the hallmark of an index that orders by "
        f"(created_at, id) before it restricts by workspace_id"
    )


# --------------------------------------------------------------------------
# H. Delete safety
# --------------------------------------------------------------------------


async def test_deleting_a_team_that_still_owns_issues_is_refused(connection):
    """The single statement that CASCADE would turn into total data loss.

    After the backfill every issue in the database belongs to the one
    bootstrap team, so `DELETE FROM teams WHERE id = ...` under CASCADE
    removes all of them while the command tag reads `DELETE 1`.

    `RestrictViolationError` rather than `ForeignKeyViolationError`, and the
    difference is not a detail of asyncpg's class names. PostgreSQL raises
    SQLSTATE 23001 for an explicit `ON DELETE RESTRICT` and 23503 for
    `NO ACTION`, and asyncpg maps them to two classes that are siblings under
    `IntegrityConstraintViolationError` -- neither is a subclass of the other.
    So this assertion draws the very line 002's own comment draws when it
    explains that NO ACTION would be equivalent here and differs only in being
    deferrable. CASCADE is caught more bluntly: it raises nothing at all, and
    `pytest.raises` fails on the empty context.

    The constraint *name* is asserted as well as the class, because every
    RESTRICT in the schema raises this same exception -- without the name this
    would pass if some entirely different constraint were the one refusing.
    """
    with pytest.raises(asyncpg.RestrictViolationError) as error:
        await connection.execute(
            "DELETE FROM teams WHERE id = $1",
            BOOTSTRAP_TEAM_ID,
        )

    assert "issues_team_fk" in str(error.value)

    surviving_team = await connection.fetchval(
        "SELECT count(*) FROM teams WHERE id = $1",
        BOOTSTRAP_TEAM_ID,
    )

    assert await connection.fetchval("SELECT count(*) FROM issues") == len(SEED)
    assert surviving_team == 1


async def test_deleting_a_workspace_that_still_owns_a_team_is_refused(connection):
    """The same claim one level up; see the note on the class above."""
    with pytest.raises(asyncpg.RestrictViolationError) as error:
        await connection.execute(
            "DELETE FROM workspaces WHERE id = $1",
            BOOTSTRAP_WORKSPACE_ID,
        )

    assert "teams_workspace_fk" in str(error.value)

    assert await connection.fetchval("SELECT count(*) FROM issues") == len(SEED)
    assert await connection.fetchval("SELECT count(*) FROM teams") == 1
    assert await connection.fetchval("SELECT count(*) FROM workspaces") == 1


async def test_deleting_a_team_with_no_issues_succeeds(connection):
    """The other half of the delete tests, without which they prove nothing.

    A constraint that refused every `DELETE FROM teams` -- or a table someone
    had made read-only -- satisfies both refusal tests above perfectly. This
    is what distinguishes "RESTRICT is enforced" from "deletes do not work".
    """
    await connection.execute(
        "INSERT INTO teams (id, workspace_id, name) VALUES ($1, $2, $3)",
        EMPTY_TEAM_ID,
        BOOTSTRAP_WORKSPACE_ID,
        "Empty",
    )

    await connection.execute("DELETE FROM teams WHERE id = $1", EMPTY_TEAM_ID)

    assert await connection.fetchval("SELECT count(*) FROM teams") == 1


async def test_deleting_a_workspace_with_no_teams_succeeds(connection):
    """The control the workspace-delete test lacked.

    `test_deleting_a_team_with_no_issues_succeeds` above supplies this for
    teams; workspaces had only the refusal, so a schema in which
    `DELETE FROM workspaces` never succeeded passed. RESTRICT is a claim about
    dependent rows, and a claim about dependent rows is only tested by the
    case where there are none.

    The workspace is created empty and deleted immediately: giving it a team
    would be testing the refusal again, in a test whose whole purpose is the
    other direction.
    """
    await connection.execute(
        "INSERT INTO workspaces (id, slug, name) VALUES ($1, $2, $3)",
        EMPTY_WORKSPACE_ID,
        "empty",
        "Empty",
    )

    assert await connection.fetchval("SELECT count(*) FROM workspaces") == 2

    await connection.execute(
        "DELETE FROM workspaces WHERE id = $1",
        EMPTY_WORKSPACE_ID,
    )

    assert await connection.fetchval("SELECT count(*) FROM workspaces") == 1
    assert (
        await connection.fetchval(
            "SELECT count(*) FROM workspaces WHERE id = $1",
            BOOTSTRAP_WORKSPACE_ID,
        )
        == 1
    )


# --------------------------------------------------------------------------
# I. Composite foreign key
# --------------------------------------------------------------------------


async def test_an_issue_cannot_be_inserted_into_a_team_from_another_workspace(
    two_tenants,
):
    """The reason the foreign key is composite, asserted in raw SQL.

    Nothing from `app/` is imported here on purpose. The application will
    grow a service that refuses this pair, and a test that went through it
    would be testing that service; writing the INSERT by hand *is* the bypass
    being tested, and the database is the only layer that can still say no.

    This is the test that must turn red if `issues_team_fk` is ever replaced
    by a single-column `REFERENCES teams (id)`: under that constraint team B
    exists, so the row inserts cleanly and the issue lives in workspace A
    while pointing at a team in workspace B.
    """
    with pytest.raises(asyncpg.ForeignKeyViolationError) as error:
        await two_tenants.execute(
            """
            INSERT INTO issues (workspace_id, team_id, title, priority)
            VALUES ($1, $2, $3, $4)
            """,
            BOOTSTRAP_WORKSPACE_ID,
            OTHER_TEAM_ID,
            "cross-tenant insert",
            0,
        )

    assert "issues_team_fk" in str(error.value)

    smuggled = await two_tenants.fetchval(
        "SELECT count(*) FROM issues WHERE title = $1",
        "cross-tenant insert",
    )

    assert smuggled == 0
    assert await two_tenants.fetchval("SELECT count(*) FROM issues") == len(SEED)


async def test_an_issue_inserts_cleanly_into_a_matching_workspace_and_team_pair(
    two_tenants,
):
    """Without this, a constraint that rejected every insert would pass above."""
    await two_tenants.execute(
        """
        INSERT INTO issues (workspace_id, team_id, title, priority)
        VALUES ($1, $2, $3, $4)
        """,
        OTHER_WORKSPACE_ID,
        OTHER_TEAM_ID,
        "matching pair",
        0,
    )

    filed = await two_tenants.fetchval(
        "SELECT count(*) FROM issues WHERE workspace_id = $1",
        OTHER_WORKSPACE_ID,
    )

    assert filed == 1


async def test_an_issue_cannot_be_moved_into_another_workspace_by_update(two_tenants):
    """The UPDATE direction, which is a separate hole from the INSERT one.

    A constraint enforced only on insert would let a single
    `UPDATE issues SET workspace_id = ...` relocate a row into a tenant whose
    teams it has nothing to do with -- the same end state the insert test
    forbids, reached by the statement nobody thought to check.
    """
    moved = SEED_BY_ID[0].id

    with pytest.raises(asyncpg.ForeignKeyViolationError) as error:
        await two_tenants.execute(
            "UPDATE issues SET workspace_id = $1 WHERE id = $2",
            OTHER_WORKSPACE_ID,
            moved,
        )

    assert "issues_team_fk" in str(error.value)

    still_owned_by = await two_tenants.fetchval(
        "SELECT workspace_id FROM issues WHERE id = $1",
        moved,
    )

    assert still_owned_by == BOOTSTRAP_WORKSPACE_ID


async def test_an_issue_can_be_moved_to_another_workspaces_team_as_a_matching_pair(
    two_tenants,
):
    """The positive control for the UPDATE test above, and it was missing.

    Every `UPDATE issues` in this file was inside a `pytest.raises` until this
    test existed, which meant a schema where *no* update to issues could
    succeed satisfied the suite completely. A constraint that refuses
    everything is not the constraint 002 declares, and the difference is
    invisible without a case that must be allowed.

    Both columns move together, which is the point: `issues_team_fk` is a
    claim about the *pair*, so relocating an issue to another tenant is
    legitimate exactly when its team moves with it. That is the same statement
    the test above rejects, differing only in whether the team belongs to the
    workspace named alongside it.
    """
    moved = SEED_BY_ID[0].id

    await two_tenants.execute(
        "UPDATE issues SET workspace_id = $1, team_id = $2 WHERE id = $3",
        OTHER_WORKSPACE_ID,
        OTHER_TEAM_ID,
        moved,
    )

    row = await two_tenants.fetchrow(
        "SELECT workspace_id, team_id FROM issues WHERE id = $1",
        moved,
    )

    assert row["workspace_id"] == OTHER_WORKSPACE_ID
    assert row["team_id"] == OTHER_TEAM_ID

    # The rest of the table did not move with it: the UPDATE named one row and
    # the FK is per-row, so a constraint that somehow rewrote its neighbours
    # would show here.
    assert (
        await two_tenants.fetchval(
            "SELECT count(*) FROM issues WHERE workspace_id = $1",
            BOOTSTRAP_WORKSPACE_ID,
        )
        == len(SEED) - 1
    )


# --------------------------------------------------------------------------
# J. Ledger and re-application
# --------------------------------------------------------------------------


async def test_the_ledger_records_001_and_002_with_their_file_checksums(applied):
    """Both rows, because 001's is the adoption the fixture's genealogy forces.

    An empty ledger over a table that matches 001 is the state
    `_adopt_initial_migration` exists for, so applying 002 through the runner
    writes 001's row first. Asserting only 002's would leave the adoption path
    -- the one that runs against production the first time anyone applies
    anything -- unexercised.

    The ledger has no `name` column: it is `version` / `applied_at` /
    `checksum` (scripts/apply_migration.py:60-66).
    """
    rows = await applied.connection.fetch(
        "SELECT version, checksum FROM schema_migrations ORDER BY version"
    )

    assert [tuple(row) for row in rows] == [
        ("001", compute_checksum(read_migration(MIGRATION_001))),
        ("002", compute_checksum(read_migration(MIGRATION_002))),
    ]

    assert "002" in applied.message
    assert "already applied" not in applied.message


async def test_the_ledger_timestamps_are_timezone_aware_and_from_the_server_clock(
    connection,
):
    """Compared against `now()` read from the server, never Python's clock.

    A container's clock and the test runner's are two different clocks, and
    nothing keeps them together. An exact value is never asserted -- the
    column defaults to `now()`, which is transaction start time, and pinning
    it would be a test of the fixture's speed.
    """
    rows = await connection.fetch(
        "SELECT version, applied_at FROM schema_migrations ORDER BY version"
    )
    server_now = await connection.fetchval("SELECT now()")

    assert len(rows) == 2

    for row in rows:
        applied_at = row["applied_at"]

        assert applied_at is not None, row["version"]
        assert applied_at.tzinfo is not None, row["version"]
        assert applied_at.utcoffset() is not None, row["version"]
        assert applied_at <= server_now, row["version"]
        assert server_now - applied_at < timedelta(minutes=5), row["version"]


async def test_migration_status_reports_both_versions_applied_and_nothing_pending(
    connection,
):
    """The runner's own view of the database it has just changed.

    `migration_status` writes -- the ledger prologue creates the table and can
    adopt 001 -- so it needs a transaction as much as applying does.
    """
    async with connection.transaction():
        report = await migration_status(connection, migrations_dir=MIGRATIONS_DIR)

    assert [item.version for item in report.applied] == ["001", "002"]
    assert [item.state for item in report.applied] == [CHECKSUM_OK, CHECKSUM_OK]
    assert report.pending == ()
    assert report.has_mismatch is False


async def test_re_applying_002_executes_nothing_and_changes_nothing(applied):
    """The refusal that makes the ledger worth having.

    002 is not idempotent and cannot be: it has no `IF NOT EXISTS` anywhere,
    its two bootstrap INSERTs would collide on the primary key, and
    `DROP INDEX issues_created_at_id_idx` would fail on an index that is
    already gone. Nothing in the file protects it -- the ledger is the only
    thing standing between a second `apply_migration` call and a failed
    transaction.

    `applied_at` is captured and compared rather than merely re-read. A runner
    that upserted its ledger row would leave the row count at two and every
    other assertion here passing, and the moved timestamp is the only trace it
    would leave.
    """
    connection = applied.connection

    before_applied_at = await connection.fetchval(
        "SELECT applied_at FROM schema_migrations WHERE version = $1",
        "002",
    )

    async with connection.transaction():
        message = await apply_migration(
            connection,
            MIGRATION_002,
            migrations_dir=MIGRATIONS_DIR,
        )

    ledger_rows = await connection.fetchval(
        "SELECT count(*) FROM schema_migrations WHERE version = $1",
        "002",
    )
    after_applied_at = await connection.fetchval(
        "SELECT applied_at FROM schema_migrations WHERE version = $1",
        "002",
    )

    assert "already applied" in message
    assert "002" in message

    assert ledger_rows == 1
    assert after_applied_at == before_applied_at

    assert await connection.fetchval("SELECT count(*) FROM workspaces") == 1
    assert await connection.fetchval("SELECT count(*) FROM teams") == 1
    assert await _snapshot(connection) == SEED_TUPLES
