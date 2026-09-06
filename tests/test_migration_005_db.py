"""Migration 005 applied by the real runner, over rows that predate it.

`test_migration_lint.py` reads 005 as text and `test_issue_number_concurrency_db.py`
exercises the allocator it installs. Neither answers the questions this file
asks, because both are arguments about something other than the schema that
comes out the far end:

  * two backfills run here over rows that existed before any of this -- issue
    numbers, and a workflow state per issue -- and a backfill is only correct
    against rows that were already there;
  * `UNIQUE (workspace_id, key)` and `UNIQUE (key)` are one word apart in the
    file and are the difference between two tenants each having an ENG team
    and one tenant's choice denying it to every other;
  * a composite foreign key over `(workspace_id, team_id, workflow_state_id)`
    and three single-column ones are indistinguishable in every catalog
    summary that does not read the column ORDER, and behave identically until
    someone moves an issue into another team's workflow state.

So this file builds the production genealogy rather than the end state: 001's
schema, populated, then 002 and then 005 through `scripts.apply_migration`
exactly as an operator would. Everything afterwards is asked of the server.

The disciplines are borrowed from `test_migration_002_db.py`: ids come from a
suffix scheme whose hex order is its decimal order, and expected values are
computed in Python from the seed literals rather than by re-running the query
under test.

Marked `db`: deselected by default, skipped when Docker is unreachable.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from operator import attrgetter
from pathlib import Path
from uuid import UUID

import asyncpg
import pytest

from scripts.apply_migration import apply_migration, read_migration

from tests.conftest import reset_schema


pytestmark = pytest.mark.db

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"
MIGRATION_001 = MIGRATIONS_DIR / "001_issues.sql"
MIGRATION_002 = MIGRATIONS_DIR / "002_tenancy.sql"
MIGRATION_005 = MIGRATIONS_DIR / "005_team_workflows.sql"

BOOTSTRAP_WORKSPACE_ID = UUID("00000000-0000-7000-8000-000000000001")
BOOTSTRAP_TEAM_ID = UUID("00000000-0000-7000-8000-000000000002")

# The key 005 gives the bootstrap team, written as a literal because the
# migration writes it as one.
BOOTSTRAP_TEAM_KEY = "CORE"

# A second tenant with a team of its own, created by the tests. It is what
# makes "the same key in another workspace" and "a state belonging to another
# team" expressible at all.
OTHER_WORKSPACE_ID = UUID("00000000-0000-7000-8000-0000000000b1")
OTHER_TEAM_ID = UUID("00000000-0000-7000-8000-0000000000b2")

# A second team in the BOOTSTRAP workspace, for the per-workspace uniqueness
# tests. Same workspace is the point.
SIBLING_TEAM_ID = UUID("00000000-0000-7000-8000-0000000000c1")
REJECTED_TEAM_ID = UUID("00000000-0000-7000-8000-0000000000c2")
REJECTED_STATE_ID = UUID("00000000-0000-7000-8000-0000000000c3")

# The five categories 005 constrains `workflow_states.type` to, in the order
# the seed positions them.
SEEDED_WORKFLOW = (
    ("Backlog", "backlog", 0, "#bec2c8"),
    ("Todo", "unstarted", 1, "#e2e2e2"),
    ("In Progress", "started", 2, "#f2c94c"),
    ("Done", "completed", 3, "#5e6ad2"),
    ("Canceled", "canceled", 4, "#95a2b3"),
)

CATEGORIES = tuple(category for _, category, _, _ in SEEDED_WORKFLOW)

WORKFLOW_STATE_INDEX = "issues_workflow_state_idx"

# Referential action bytes, pinned as bytes rather than read through
# pg_get_constraintdef, for the reason test_migration_002_db.py gives: the byte
# is what the executor consults.
RESTRICT = "r"
MATCH_SIMPLE = "s"

# The columns 001 owns, which 005 must not have touched.
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

# 002's two, which 005 must not have touched either.
TENANCY_COLUMNS_FROM_002 = frozenset({"workspace_id", "team_id"})

# The tables 005 changes, fingerprinted the way scripts/apply_migration.py
# fingerprints the table 001 creates: column -> (type as information_schema
# spells it, nullable, normalized default). A name set is not a schema.
#
# The `None` defaults are the load-bearing entries. A DEFAULT on
# `issues.number` would make every insert that forgets a number land on the
# same one and collide; a DEFAULT on `issues.workflow_state_id` would put every
# issue that forgets a state into one particular team's board, across tenants.
# `teams.issue_counter` is the exception that proves it: 0 is the right answer
# for a team that has allocated nothing, and the migration argues for it.
TEAM_COLUMNS = {
    "id": ("uuid", False, "uuidv7()"),
    "workspace_id": ("uuid", False, None),
    "name": ("text", False, None),
    "created_at": ("timestamp with time zone", False, "now()"),
    "key": ("text", False, None),
    "issue_counter": ("bigint", False, "0"),
}

WORKFLOW_STATE_COLUMNS = {
    "id": ("uuid", False, "uuidv7()"),
    "workspace_id": ("uuid", False, None),
    "team_id": ("uuid", False, None),
    "name": ("text", False, None),
    "type": ("text", False, None),
    "position": ("integer", False, None),
    "color": ("text", True, None),
    "created_at": ("timestamp with time zone", False, "now()"),
}

COLUMNS_ADDED_TO_ISSUES = {
    "number": ("bigint", False, None),
    "workflow_state_id": ("uuid", False, None),
}

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

# Constraints by structure. `conkey`/`confkey` are unnested WITH ORDINALITY and
# resolved to column names in order, because the order is most of the
# assertion; see the same query in test_migration_002_db.py.
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

DEFAULT_CAST = re.compile(r'::[a-z0-9_ ."]+')

SEED_DAY = (2026, 1, 1)
UPDATED_BASE = datetime(2026, 2, 2, 8, 0, 0, tzinfo=timezone.utc)


def _at(hour: int, minute: int, second: int, microsecond: int = 0) -> datetime:
    return datetime(*SEED_DAY, hour, minute, second, microsecond, tzinfo=timezone.utc)


# Two rows share this timestamp to the microsecond. That is what makes 005's
# `ORDER BY created_at, id` in the numbering backfill load-bearing rather than
# decorative: without the id tiebreak, which of the two gets the lower number
# is whatever the scan happened to produce.
TIED = _at(12, 5, 0, 123456)


def _issue_id(suffix: int) -> UUID:
    """A UUID whose sort position follows `suffix`; see test_migration_002_db."""
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
    priority: int = 1,
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


# Insertion order is a deliberate scramble: neither id order, nor created_at
# order, nor either reversed. Nothing about heap order can stand in for the
# ordering 005's backfill declares.
#
# Two rows carry `completed_at`, which is what the workflow_state_id backfill
# branches on, and they are not adjacent in any of the orderings above.
SEED = (
    _row(60, _at(12, 4, 0), title="Café — impossible d'ouvrir"),
    _row(30, _at(12, 9, 0, 500000), priority=2, completed_at=_at(13, 0, 0)),
    _row(90, _at(12, 6, 0), priority=4),
    _row(10, _at(12, 8, 0), priority=3, description="second newest"),
    _row(80, TIED, priority=2, description="tied, higher id"),
    _row(40, _at(12, 2, 0, 999999), description="microsecond edge"),
    _row(70, TIED, description=""),
    _row(20, _at(12, 1, 0), priority=0, completed_at=_at(14, 0, 0)),
    _row(50, _at(12, 3, 0), priority=4, description="oldest of the middle band"),
)

SEED_BY_ID = sorted(SEED, key=attrgetter("id"))
SEED_TUPLES = [row.as_tuple() for row in SEED_BY_ID]

# The numbering 005 must produce, computed here from the seed literals by
# sorting them the way the migration says it sorts, never by asking the
# database what it wrote.
EXPECTED_NUMBERS = {
    row.id: number
    for number, row in enumerate(
        sorted(SEED, key=lambda row: (row.created_at, row.id)),
        start=1,
    )
}

COMPLETED_SEED_IDS = frozenset(row.id for row in SEED if row.completed_at is not None)


@dataclass(frozen=True, slots=True)
class Applied:
    connection: asyncpg.Connection
    before: list[tuple]


def _normalize_sql(text: str) -> str:
    return " ".join(text.split()).lower()


def _normalize_default(expression: str | None) -> str | None:
    """The same reduction as scripts/apply_migration.py `_normalize_default`."""
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
    rows = await connection.fetch(COLUMNS_SQL, table)

    return {
        row["column_name"]: (
            row["data_type"],
            row["is_nullable"] == "YES",
            _normalize_default(row["column_default"]),
        )
        for row in rows
    }


async def _constraints(connection, table: str) -> dict[str, dict]:
    rows = await connection.fetch(CONSTRAINTS_SQL, table)

    return {row["conname"]: dict(row) for row in rows}


async def _index_structure(connection, index: str) -> list[dict]:
    return [dict(row) for row in await connection.fetch(INDEX_SQL, index)]


async def _snapshot(connection) -> list[tuple]:
    return [tuple(row) for row in await connection.fetch(SNAPSHOT_SQL)]


async def _state_id(connection, team_id: UUID, category: str) -> UUID:
    state_id = await connection.fetchval(
        "SELECT id FROM workflow_states WHERE team_id = $1 AND type = $2",
        team_id,
        category,
    )

    assert state_id is not None, f"no {category} state on team {team_id}"

    return state_id


@pytest.fixture
async def applied(postgres_dsn):
    """001's schema, populated, then 002 and 005 through the real runner.

    `workflow_states` joins the tables dropped first. The container is shared
    for the whole session, so leaving it behind would make the next run of this
    file fail inside 005's `CREATE TABLE workflow_states` -- a failure with
    nothing to do with the migration.
    """
    connection = await asyncpg.connect(postgres_dsn)

    try:
        await reset_schema(connection)
        await connection.execute(read_migration(MIGRATION_001))
        await connection.executemany(
            INSERT_ISSUE_SQL,
            [row.as_tuple() for row in SEED],
        )

        before = await _snapshot(connection)

        for migration in (MIGRATION_002, MIGRATION_005):
            async with connection.transaction():
                await apply_migration(
                    connection,
                    migration,
                    migrations_dir=MIGRATIONS_DIR,
                )

        yield Applied(connection=connection, before=before)
    finally:
        await connection.close()


@pytest.fixture
def connection(applied: Applied) -> asyncpg.Connection:
    return applied.connection


@pytest.fixture
async def two_tenants(connection) -> asyncpg.Connection:
    """A second workspace with a team of its own.

    005 gives keys and workflows to the teams it finds; a team created
    afterwards is the case the application will actually produce, and it is
    what makes "the same key in another workspace" expressible.
    """
    await connection.execute(
        "INSERT INTO workspaces (id, slug, name) VALUES ($1, 'beta', 'Beta')",
        OTHER_WORKSPACE_ID,
    )
    await connection.execute(
        "INSERT INTO teams (id, workspace_id, name, key) VALUES ($1, $2, $3, $4)",
        OTHER_TEAM_ID,
        OTHER_WORKSPACE_ID,
        "Beta Core",
        BOOTSTRAP_TEAM_KEY,
    )

    return connection


# --------------------------------------------------------------------------
# A. Data preservation
# --------------------------------------------------------------------------


async def test_005_preserves_every_pre_existing_issue_column_for_column(applied):
    """The nine seeded rows, all seven of their 001 columns, unchanged.

    Compared as lists rather than sets, and never as a digest: a set absorbs a
    duplicated row silently, and a checksum says that something changed while
    refusing to say which column of which row.

    The before-snapshot is checked against the seed literals as well as against
    the after-snapshot, because `before == after` also passes when the fixture
    seeded nothing and both are empty.
    """
    after = await _snapshot(applied.connection)

    assert applied.before == SEED_TUPLES, (
        "the fixture did not put the seed rows in the table, so the "
        "preservation comparison below would prove nothing"
    )

    assert len(after) == len(SEED)
    assert after == applied.before


# --------------------------------------------------------------------------
# B. Team keys
# --------------------------------------------------------------------------


async def test_the_bootstrap_team_was_given_the_key_the_migration_names(connection):
    row = await connection.fetchrow(
        "SELECT key, issue_counter FROM teams WHERE id = $1",
        BOOTSTRAP_TEAM_ID,
    )

    assert row["key"] == BOOTSTRAP_TEAM_KEY
    assert row["issue_counter"] == len(SEED)


async def test_every_team_has_a_key_and_no_two_in_one_workspace_share_it(connection):
    rows = await connection.fetch("SELECT workspace_id, key FROM teams")
    pairs = [(row["workspace_id"], row["key"]) for row in rows]

    assert all(key is not None for _, key in pairs)
    assert len(set(pairs)) == len(pairs)


async def test_two_teams_in_one_workspace_cannot_share_a_key(connection):
    """The constraint in its narrow scope: same workspace, same key, refused."""
    with pytest.raises(asyncpg.UniqueViolationError) as error:
        await connection.execute(
            "INSERT INTO teams (id, workspace_id, name, key) VALUES ($1, $2, $3, $4)",
            SIBLING_TEAM_ID,
            BOOTSTRAP_WORKSPACE_ID,
            "Core Again",
            BOOTSTRAP_TEAM_KEY,
        )

    assert "teams_workspace_key_unique" in str(error.value)


async def test_the_same_key_in_a_different_workspace_is_accepted(two_tenants):
    """The other half of the same constraint, and the half worth stating.

    A test that only proved the rejection above would pass identically against
    a global `UNIQUE (key)`, which is the constraint 005 exists not to declare:
    it would make one tenant's choice of team key deny it to every other, and
    turn a failed create into a disclosure that some unrelated workspace holds
    that key.
    """
    keys = await two_tenants.fetch(
        "SELECT workspace_id FROM teams WHERE key = $1 ORDER BY workspace_id",
        BOOTSTRAP_TEAM_KEY,
    )

    assert [row["workspace_id"] for row in keys] == [
        BOOTSTRAP_WORKSPACE_ID,
        OTHER_WORKSPACE_ID,
    ]


@pytest.mark.parametrize(
    "key",
    [
        "eng",  # lowercase: UNIQUE on TEXT is case-sensitive, so 'eng' and
        "Eng",  # 'ENG' would be two teams whose identifiers collide on paper
        "EN-G",  # a hyphen makes `EN-G-42` unparseable as <key>-<number>
        "1ENG",  # leading digit, the mirror image of the same problem
        "",  # a key that renders as nothing
        "ENGINEERING1",  # longer than the format admits
        "ENG ",  # trailing space, invisible in every UI that shows it
    ],
)
async def test_the_key_format_check_rejects_what_it_was_written_to_reject(
    connection,
    key,
):
    with pytest.raises(asyncpg.CheckViolationError) as error:
        await connection.execute(
            "INSERT INTO teams (id, workspace_id, name, key) VALUES ($1, $2, $3, $4)",
            REJECTED_TEAM_ID,
            BOOTSTRAP_WORKSPACE_ID,
            "Rejected",
            key,
        )

    assert "teams_key_format" in str(error.value)


# --------------------------------------------------------------------------
# C. Issue numbers
# --------------------------------------------------------------------------


async def test_existing_issues_were_numbered_in_created_at_then_id_order(connection):
    """The backfill, checked against numbers computed in Python from the seed.

    Two of the seed rows share `created_at` to the microsecond, so this also
    settles the tiebreak: without `ORDER BY created_at, id` the two tied rows
    could be numbered either way round and the migration would produce
    different identifiers on two copies of the same database.
    """
    rows = await connection.fetch("SELECT id, number FROM issues")

    assert {row["id"]: row["number"] for row in rows} == EXPECTED_NUMBERS


async def test_the_counter_was_moved_past_every_number_already_handed_out(connection):
    """Leaving the counter at 0 would collide the next create with the oldest
    issue on the team, which is worse than not numbering at all."""
    counter = await connection.fetchval(
        "SELECT issue_counter FROM teams WHERE id = $1",
        BOOTSTRAP_TEAM_ID,
    )
    highest = await connection.fetchval(
        "SELECT max(number) FROM issues WHERE team_id = $1",
        BOOTSTRAP_TEAM_ID,
    )

    assert counter == highest == len(SEED)


async def test_a_team_with_no_issues_keeps_the_counter_default(two_tenants):
    counter = await two_tenants.fetchval(
        "SELECT issue_counter FROM teams WHERE id = $1",
        OTHER_TEAM_ID,
    )

    assert counter == 0


async def test_the_issue_number_unique_key_is_scoped_to_the_team(connection):
    constraint = (await _constraints(connection, "public.issues"))[
        "issues_team_number_key"
    ]

    assert constraint["contype"] == "u"
    assert constraint["local_columns"] == ["team_id", "number"]


async def test_number_zero_is_refused(connection):
    """`issues_number_positive` exists because 0 is what an uninitialised
    counter, a `coalesce(max, 0)` without the `+ 1`, or an off-by-one import
    all produce, and ENG-0 is not an identifier anyone means to create."""
    state_id = await _state_id(connection, BOOTSTRAP_TEAM_ID, "unstarted")

    with pytest.raises(asyncpg.CheckViolationError) as error:
        await connection.execute(
            """
            INSERT INTO issues
                (workspace_id, team_id, workflow_state_id, number, title, priority)
            VALUES ($1, $2, $3, 0, 'zero', 0)
            """,
            BOOTSTRAP_WORKSPACE_ID,
            BOOTSTRAP_TEAM_ID,
            state_id,
        )

    assert "issues_number_positive" in str(error.value)


# --------------------------------------------------------------------------
# D. Workflow states
# --------------------------------------------------------------------------


async def test_every_team_was_seeded_the_default_workflow_in_order(connection):
    rows = await connection.fetch(
        """
        SELECT name, type, position, color
        FROM workflow_states
        WHERE team_id = $1
        ORDER BY position
        """,
        BOOTSTRAP_TEAM_ID,
    )

    assert [tuple(row) for row in rows] == list(SEEDED_WORKFLOW)


async def test_the_seed_covers_every_category_exactly_once_per_team(connection):
    """One state per category is what the workflow_state_id backfill relies on.

    With two 'unstarted' states on a team, `UPDATE ... FROM workflow_states`
    would match twice and PostgreSQL would pick one arbitrarily -- a backfill
    whose result is not reproducible. With none, the SET NOT NULL would abort
    the migration. The seed is what makes both impossible.
    """
    rows = await connection.fetch(
        "SELECT type, count(*) AS n FROM workflow_states GROUP BY type ORDER BY type"
    )

    teams = await connection.fetchval("SELECT count(*) FROM teams")

    assert {row["type"]: row["n"] for row in rows} == {
        category: teams for category in CATEGORIES
    }


async def test_a_category_outside_the_vocabulary_is_refused(connection):
    """The CHECK is what keeps `type` a closed set that code can branch on.

    'in_progress' is the plausible wrong value: it is the name of a default
    state, and a caller who confuses the name with the category writes exactly
    this.
    """
    with pytest.raises(asyncpg.CheckViolationError) as error:
        await connection.execute(
            """
            INSERT INTO workflow_states
                (id, workspace_id, team_id, name, type, position)
            VALUES ($1, $2, $3, 'Doing', 'in_progress', 9)
            """,
            REJECTED_STATE_ID,
            BOOTSTRAP_WORKSPACE_ID,
            BOOTSTRAP_TEAM_ID,
        )

    assert "workflow_states_type_check" in str(error.value)


async def test_a_blank_state_name_is_refused(connection):
    with pytest.raises(asyncpg.CheckViolationError) as error:
        await connection.execute(
            """
            INSERT INTO workflow_states
                (id, workspace_id, team_id, name, type, position)
            VALUES ($1, $2, $3, '   ', 'started', 9)
            """,
            REJECTED_STATE_ID,
            BOOTSTRAP_WORKSPACE_ID,
            BOOTSTRAP_TEAM_ID,
        )

    assert "workflow_states_name_present" in str(error.value)


async def test_two_states_on_one_team_cannot_share_a_name(connection):
    with pytest.raises(asyncpg.UniqueViolationError) as error:
        await connection.execute(
            """
            INSERT INTO workflow_states
                (id, workspace_id, team_id, name, type, position)
            VALUES ($1, $2, $3, 'Todo', 'backlog', 9)
            """,
            REJECTED_STATE_ID,
            BOOTSTRAP_WORKSPACE_ID,
            BOOTSTRAP_TEAM_ID,
        )

    assert "workflow_states_team_name_key" in str(error.value)


async def test_two_states_on_one_team_may_share_a_position(connection):
    """The absence of a unique constraint on position is a decision, so it is
    pinned.

    A unique `(team_id, position)` makes the ordinary reorder -- swap two
    adjacent states -- impossible in one statement, because the intermediate
    state of any swap collides. `ORDER BY position, id` is total regardless,
    which is what the repository orders by.
    """
    await connection.execute(
        """
        INSERT INTO workflow_states
            (id, workspace_id, team_id, name, type, position)
        VALUES ($1, $2, $3, 'Triage', 'backlog', 0)
        """,
        REJECTED_STATE_ID,
        BOOTSTRAP_WORKSPACE_ID,
        BOOTSTRAP_TEAM_ID,
    )

    sharing = await connection.fetchval(
        "SELECT count(*) FROM workflow_states WHERE team_id = $1 AND position = 0",
        BOOTSTRAP_TEAM_ID,
    )

    assert sharing == 2


async def test_a_workflow_state_cannot_belong_to_another_workspaces_team(two_tenants):
    """The composite FK on workflow_states, exercised rather than read.

    The pair `(workspace_id, team_id)` must name a real team. Two single-column
    keys would each pass while together describing a state in workspace A
    attached to a team in workspace B.
    """
    with pytest.raises(asyncpg.ForeignKeyViolationError) as error:
        await two_tenants.execute(
            """
            INSERT INTO workflow_states
                (id, workspace_id, team_id, name, type, position)
            VALUES ($1, $2, $3, 'Smuggled', 'started', 9)
            """,
            REJECTED_STATE_ID,
            BOOTSTRAP_WORKSPACE_ID,
            OTHER_TEAM_ID,
        )

    assert "workflow_states_team_fk" in str(error.value)


# --------------------------------------------------------------------------
# E. issues.workflow_state_id
# --------------------------------------------------------------------------


async def test_existing_issues_were_placed_by_their_completion_state(connection):
    """The backfill mapped `completed_at` onto a category, not onto a name.

    Two of the nine seed rows are completed and they are not adjacent in any
    ordering the migration uses, so a backfill that put everything in one state
    fails here rather than passing on a fixture that could not tell.
    """
    rows = await connection.fetch(
        """
        SELECT issues.id, state.type AS category
        FROM issues
        JOIN workflow_states AS state ON state.id = issues.workflow_state_id
        """
    )

    placed = {row["id"]: row["category"] for row in rows}

    assert placed == {
        row.id: ("completed" if row.id in COMPLETED_SEED_IDS else "unstarted")
        for row in SEED
    }

    assert len(COMPLETED_SEED_IDS) == 2, (
        "the seed no longer contains completed rows, so the mapping above is "
        "satisfied by putting every issue in one state"
    )


async def test_the_workflow_state_foreign_key_is_composite_ordered_and_restricting(
    connection,
):
    """Every clause here is a separate way for the schema to look right and
    behave wrongly.

      * the local columns in order, because any other order is a different
        constraint that an unordered comparison accepts;
      * the referenced columns in order, for the same reason on the other side;
      * the referenced table, because a FK onto `teams` would satisfy a
        name-only check while enforcing nothing about states;
      * the action bytes, because RESTRICT and CASCADE are one word apart in
        the file and the difference is whether deleting a workflow state
        refuses or deletes every issue sitting in it;
      * `condeferrable`, because a deferred constraint is not checked until
        COMMIT, and every test in this file runs in autocommit -- so a
        DEFERRABLE INITIALLY DEFERRED version would pass all of them while
        letting a service that opens an explicit transaction write and read
        back a cross-team issue inside it;
      * `confmatchtype`, because MATCH SIMPLE is what makes the NOT NULL on
        workflow_state_id load-bearing: a NULL there would switch the whole
        tuple check off for that row.
    """
    constraint = (await _constraints(connection, "public.issues"))[
        "issues_workflow_state_fk"
    ]

    assert constraint["contype"] == "f"
    assert constraint["local_columns"] == [
        "workspace_id",
        "team_id",
        "workflow_state_id",
    ]
    assert constraint["referenced_table"] == "workflow_states"
    assert constraint["foreign_columns"] == ["workspace_id", "team_id", "id"]
    assert constraint["delete_action"] == RESTRICT
    assert constraint["update_action"] == RESTRICT
    assert constraint["match_type"] == MATCH_SIMPLE
    assert constraint["deferrable"] is False
    assert constraint["deferred"] is False


async def test_an_issue_cannot_sit_in_another_teams_workflow_state(connection):
    """The constraint this section exists for, exercised.

    Both teams are in the same workspace, so nothing but the composite key
    separates them: the issue's workspace is right, the state exists, and the
    state belongs to a different team.
    """
    await connection.execute(
        "INSERT INTO teams (id, workspace_id, name, key) VALUES ($1, $2, $3, $4)",
        SIBLING_TEAM_ID,
        BOOTSTRAP_WORKSPACE_ID,
        "Design",
        "DES",
    )
    await connection.execute(
        """
        INSERT INTO workflow_states
            (id, workspace_id, team_id, name, type, position)
        VALUES ($1, $2, $3, 'Todo', 'unstarted', 0)
        """,
        REJECTED_STATE_ID,
        BOOTSTRAP_WORKSPACE_ID,
        SIBLING_TEAM_ID,
    )

    with pytest.raises(asyncpg.ForeignKeyViolationError) as error:
        await connection.execute(
            """
            INSERT INTO issues
                (workspace_id, team_id, workflow_state_id, number, title, priority)
            VALUES ($1, $2, $3, 999, 'wrong board', 0)
            """,
            BOOTSTRAP_WORKSPACE_ID,
            BOOTSTRAP_TEAM_ID,
            REJECTED_STATE_ID,
        )

    assert "issues_workflow_state_fk" in str(error.value)


async def test_deleting_a_workflow_state_that_issues_occupy_is_refused(connection):
    """ON DELETE RESTRICT, exercised rather than read off the catalog byte.

    Under CASCADE this statement would delete every issue in the state and
    report `DELETE 1`.

    The exception type is a second, independent witness to the action byte.
    RESTRICT raises SQLSTATE 23001, which asyncpg maps to
    `RestrictViolationError`; NO ACTION -- the other refusing option, and the
    one a reviewer might read as equivalent -- raises 23503 and arrives as
    `ForeignKeyViolationError`. So this assertion distinguishes the two
    without consulting `confdeltype` at all.
    """
    state_id = await _state_id(connection, BOOTSTRAP_TEAM_ID, "unstarted")

    with pytest.raises(asyncpg.RestrictViolationError) as error:
        await connection.execute(
            "DELETE FROM workflow_states WHERE id = $1",
            state_id,
        )

    assert "issues_workflow_state_fk" in str(error.value)
    assert await connection.fetchval("SELECT count(*) FROM issues") == len(SEED)


# --------------------------------------------------------------------------
# F. Column sets
# --------------------------------------------------------------------------


async def test_the_tables_005_touches_carry_exactly_the_approved_columns(connection):
    """Dict equality in both directions: name, type, nullability and default.

    A name set is not a schema. Every one of these passes a name-set check
    unchanged: a nullable `teams.key` (UNIQUE permits many NULLs and the format
    CHECK is NULL-rather-than-false, so unlimited keyless teams insert
    cleanly); a nullable `workflow_states.team_id` (MATCH SIMPLE skips the FK
    for any row with a NULL); an `issue_counter` that lost its DEFAULT (every
    team-creating insert that omits it fails at runtime instead of at migration
    time).
    """
    assert await _column_fingerprint(connection, "teams") == TEAM_COLUMNS
    assert (
        await _column_fingerprint(connection, "workflow_states")
        == WORKFLOW_STATE_COLUMNS
    )


async def test_005_adds_exactly_number_and_workflow_state_id_to_issues(connection):
    fingerprint = await _column_fingerprint(connection, "issues")

    assert {
        name: value
        for name, value in fingerprint.items()
        if name not in ISSUE_COLUMNS_FROM_001 | TENANCY_COLUMNS_FROM_002
    } == COLUMNS_ADDED_TO_ISSUES


async def test_002s_constraints_and_index_survived_005(connection):
    """What 005 must not have disturbed while it was widening the table.

    `issues_team_fk` especially: 005 declares a second, wider composite key
    over the same two columns plus a third, and dropping or weakening the
    first in the process would leave tenancy enforced only where a workflow
    state happens to be involved.
    """
    constraints = await _constraints(connection, "public.issues")

    assert constraints["issues_team_fk"]["local_columns"] == [
        "workspace_id",
        "team_id",
    ]
    assert constraints["issues_team_fk"]["delete_action"] == RESTRICT
    assert constraints["issues_pkey"]["local_columns"] == ["id"]

    assert await _index_structure(connection, "issues_workspace_created_at_id_idx") == [
        {
            "column_name": "workspace_id",
            "is_desc": False,
            "is_unique": False,
            "is_partial": False,
            "has_expressions": False,
        },
        {
            "column_name": "created_at",
            "is_desc": True,
            "is_unique": False,
            "is_partial": False,
            "has_expressions": False,
        },
        {
            "column_name": "id",
            "is_desc": True,
            "is_unique": False,
            "is_partial": False,
            "has_expressions": False,
        },
    ]


# --------------------------------------------------------------------------
# G. Indexes
# --------------------------------------------------------------------------


async def test_the_workflow_state_index_has_the_approved_structure(connection):
    """The referencing side of `issues_workflow_state_fk`.

    PostgreSQL indexes the referenced side of a foreign key automatically and
    the referencing side never, so without this every
    `DELETE FROM workflow_states` scans issues in full to satisfy the RESTRICT
    check. The column order is the FK's column order, because that is the
    lookup the integrity check performs.
    """
    rows = await _index_structure(connection, WORKFLOW_STATE_INDEX)

    assert [(row["column_name"], row["is_desc"]) for row in rows] == [
        ("workspace_id", False),
        ("team_id", False),
        ("workflow_state_id", False),
    ]

    assert rows[0]["is_unique"] is False
    assert rows[0]["is_partial"] is False
    assert rows[0]["has_expressions"] is False
