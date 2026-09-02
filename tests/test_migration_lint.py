"""Text-only lint rules over migrations/*.sql.

No database, no Docker, no fixtures: these run anywhere.  They exist
because everything the migration runner promises -- one transaction,
applied once, gated by a ledger -- is only as strong as the SQL text
inside each file.  A file that commits halfway through makes the runner's
atomicity a fiction; a file that says IF NOT EXISTS makes the ledger's
once-only guarantee redundant *and* wrong, because defensive DDL reports
success against a schema it never actually checked.

The rules are pure functions over SQL text so they can be aimed at
deliberately-bad input.  That matters here: migrations/ currently holds a
single file that passes every rule, so a linter that always answered
"clean" would look exactly as green as a real one.

Statements are read table-qualified wherever a rule is about a specific
table.  A rule that tracked bare column names would let a backfill of
workspaces.workspace_id satisfy a constraint on issues.workspace_id --
two different columns that happen to share a name, which is exactly the
shape 002 has.

Known hole, deliberately deferred (2026-09-02)
----------------------------------------------
Rule 1 rejects a PL/pgSQL body.  ``CREATE FUNCTION f() ... AS $$ BEGIN
... END $$`` trips the no-BEGIN check, because ``_opaque_end`` already
identifies dollar-quoted regions -- it has to, so an apostrophe in a
function body cannot desynchronise the scanners -- but no rule exempts
them.  The BEGIN that opens a procedural block is not transaction
control; it cannot commit anything.

Deferring is safe *today* because no migration in the repo contains a
function body: the revised 002 has no updated_at trigger, and the
trigger it eventually needs moves to a future 003.  So the rule is
wrong about SQL that does not exist yet.

Before 003 is written, this must be fixed: exempt dollar-quoted regions
from rule 1, and prove with a test that a genuine COMMIT *outside* a
function body is still caught -- the exemption must skip the body, not
the file.  ``test_a_plpgsql_function_body_does_not_trip_rule_1`` below is
an ``xfail(strict=True)`` demonstrating the current behaviour; when
someone fixes the rule it turns green and fails the suite, which is the
signal to delete it and this note together.
"""

import re
from pathlib import Path

import pytest


MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
MIGRATION_FILES = sorted(MIGRATIONS_DIR.glob("*.sql"))


_DOLLAR_TAG = re.compile(r"\$(?:[A-Za-z_][A-Za-z_0-9]*)?\$")

_TRANSACTION_CONTROL = re.compile(
    r"\b(?:BEGIN|COMMIT|ROLLBACK|SAVEPOINT|START\s+TRANSACTION)\b",
    re.IGNORECASE,
)
_EXISTENCE_GUARD = re.compile(r"\bIF\s+(?:NOT\s+)?EXISTS\b", re.IGNORECASE)
_ON_CONFLICT = re.compile(r"\bON\s+CONFLICT\b", re.IGNORECASE)
_DESTRUCTIVE = re.compile(
    r"\bDROP\s+TABLE\b|\bDROP\s+COLUMN\b|\bTRUNCATE\b|\bDELETE\s+FROM\b",
    re.IGNORECASE,
)

_IDENTIFIER = r"(?:\"[^\"]+\"|[A-Za-z_][A-Za-z_0-9]*)"
_QUALIFIED = rf"{_IDENTIFIER}(?:\s*\.\s*{_IDENTIFIER})*"

# No trailing \b on any captured identifier below: a quoted name ends in
# '"', which is not a word character, so \b would refuse to close the
# match and "DROP \"description\"" would read as no drop at all.  The
# identifier alternation is greedy and already consumes the whole name.
_ALTER_TABLE = re.compile(
    rf"\AALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?({_QUALIFIED})",
    re.IGNORECASE,
)
_UPDATE_TARGET = re.compile(
    rf"\AUPDATE\s+(?:ONLY\s+)?({_QUALIFIED})",
    re.IGNORECASE,
)

# ALTER TABLE actions, matched against a single comma-separated action.
_SET_NOT_NULL = re.compile(
    rf"\AALTER\s+(?:COLUMN\s+)?({_IDENTIFIER})\s+SET\s+NOT\s+NULL\b",
    re.IGNORECASE,
)
_CONSTRAINT_KEYWORD = r"(?:CONSTRAINT|PRIMARY|UNIQUE|FOREIGN|CHECK|EXCLUDE)\b"
_ADD_COLUMN = re.compile(
    rf"\AADD\s+(?:COLUMN\s+|(?!{_CONSTRAINT_KEYWORD}))({_IDENTIFIER})",
    re.IGNORECASE,
)
# DROP sub-clauses that alter a column rather than discarding one.
_DROP_SUBCLAUSE = r"(?:COLUMN|CONSTRAINT|DEFAULT|NOT\s+NULL|IDENTITY|EXPRESSION)\b"
_DROP_BARE_COLUMN = re.compile(
    rf"\ADROP\s+(?!{_DROP_SUBCLAUSE})(?:IF\s+EXISTS\s+)?({_IDENTIFIER})",
    re.IGNORECASE,
)

_NOT_NULL = re.compile(r"\bNOT\s+NULL\b", re.IGNORECASE)
_DEFAULT = re.compile(r"\bDEFAULT\b", re.IGNORECASE)

_UPDATE_SET = re.compile(r"\s*UPDATE\b.*?\bSET\b", re.IGNORECASE | re.DOTALL)
_ASSIGNMENT = re.compile(rf"(?<![.\w\"])({_IDENTIFIER})\s*=", re.IGNORECASE)
_END_OF_SET_CLAUSE = re.compile(r"\b(?:WHERE|FROM|RETURNING)\b", re.IGNORECASE)


def _opaque_end(sql: str, index: int) -> int | None:
    """Index just past the string literal starting at ``index``, else None.

    Literals are skipped whole so that an apostrophe in seed data, a ';'
    inside a value, or a function body cannot desynchronise the scanners
    below and blind the linter to everything that follows.
    """
    if sql[index] == "'":
        cursor = index + 1
        while cursor < len(sql):
            if sql[cursor] != "'":
                cursor += 1
            elif sql.startswith("''", cursor):
                cursor += 2
            else:
                return cursor + 1

        return len(sql)

    tag = _DOLLAR_TAG.match(sql, index)
    if tag is None:
        return None

    close = sql.find(tag.group(), tag.end())

    return len(sql) if close == -1 else close + len(tag.group())


def strip_comments(sql: str) -> str:
    """Blank out comment text, preserving offsets and line breaks.

    A comment that names a rule ("never COMMIT in a migration") must not
    trip that rule, or the only way to document these constraints in the
    files they govern would be to break them.  Blanking rather than
    deleting keeps reported line numbers pointing at the file on disk.
    """
    out: list[str] = []
    index = 0

    while index < len(sql):
        literal_end = _opaque_end(sql, index)
        if literal_end is not None:
            out.append(sql[index:literal_end])
            index = literal_end
            continue

        if sql.startswith("--", index):
            end = sql.find("\n", index)
            end = len(sql) if end == -1 else end
        elif sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            end = len(sql) if end == -1 else end + 2
        else:
            out.append(sql[index])
            index += 1
            continue

        out.append("".join("\n" if c == "\n" else " " for c in sql[index:end]))
        index = end

    return "".join(out)


def _iter_statements(text: str):
    """(offset, statement) pairs over already-comment-stripped text.

    The offset points at the first non-space character of the statement
    in ``text``, so a rule that scans inside a statement can still report
    a line number that matches the file on disk.
    """
    start = 0
    index = 0
    bounds: list[tuple[int, str]] = []

    while index < len(text):
        literal_end = _opaque_end(text, index)
        if literal_end is not None:
            index = literal_end
            continue

        if text[index] == ";":
            bounds.append((start, text[start:index]))
            start = index + 1

        index += 1

    bounds.append((start, text[start:]))

    for offset, raw in bounds:
        statement = raw.strip()
        if statement:
            yield offset + len(raw) - len(raw.lstrip()), statement


def split_statements(sql: str) -> list[str]:
    """Comment-free statements, split on semicolons outside string literals."""
    return [statement for _, statement in _iter_statements(strip_comments(sql))]


def _top_level_parts(text: str, start: int) -> list[tuple[int, str]]:
    """(offset, text) for comma-separated parts at paren depth zero.

    Used to walk the action list of an ALTER TABLE.  Depth matters: the
    comma in ``NUMERIC(10, 2)`` does not start a new action, and the one
    in ``REFERENCES t (a, b)`` does not either.
    """
    parts: list[tuple[int, str]] = []
    depth = 0
    index = start
    part_start = start

    while index < len(text):
        literal_end = _opaque_end(text, index)
        if literal_end is not None:
            index = literal_end
            continue

        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append((part_start, text[part_start:index]))
            part_start = index + 1

        index += 1

    parts.append((part_start, text[part_start:]))

    return [
        (offset + len(raw) - len(raw.lstrip()), raw.strip())
        for offset, raw in parts
        if raw.strip()
    ]


def _matches_at_top_level(pattern: re.Pattern, text: str) -> bool:
    """Whether ``pattern`` matches outside every paren group and literal.

    ``ADD CONSTRAINT c CHECK (x IS NOT NULL)`` contains NOT NULL, but
    nested; it constrains nothing about the column's own nullability and
    must not be read as a NOT NULL column definition.
    """
    depth = 0
    index = 0

    while index < len(text):
        literal_end = _opaque_end(text, index)
        if literal_end is not None:
            index = literal_end
            continue

        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif depth == 0 and pattern.match(text, index):
            return True

        index += 1

    return False


def _report(text: str, index: int, matched: str) -> str:
    """"line N: <matched text>", so a failure names the offending SQL."""
    line = text.count("\n", 0, index) + 1

    return f"line {line}: {' '.join(matched.split())}"


def _violations(sql: str, pattern: re.Pattern) -> list[str]:
    """Each match as "line N: <matched text>", so failures name the SQL."""
    text = strip_comments(sql)

    return [_report(text, m.start(), m.group()) for m in pattern.finditer(text)]


def _normalize_identifier(identifier: str) -> str:
    return identifier.strip('"').lower()


def _normalize_table(name: str) -> str:
    """Bare table name: unquoted, lowercased, schema prefix dropped.

    Dropping the schema makes public.issues and issues the same table.
    This repo has one schema, so the alternative -- treating them as
    different -- would reject a legitimate backfill written either way,
    and a false rejection here blocks a migration that is actually safe.
    """
    return _normalize_identifier(re.findall(_IDENTIFIER, name)[-1])


def _assignments_by_update(statement: str) -> set[tuple[str, str]]:
    """(table, column) pairs an UPDATE writes, from its SET clause.

    Only the top level of SET counts: "SET a = (SELECT b WHERE c = 1)"
    writes a, not c, and "SET a = 1 WHERE b = 2" writes a, not b.
    Crediting a column that only appears in a predicate -- or in a
    subselect naming some other table -- would let a migration claim a
    backfill it never performed.

    The table is carried because column names are not unique across a
    schema: 002 adds workspace_id to more than one table.
    """
    header = _UPDATE_SET.match(statement)
    target = _UPDATE_TARGET.match(statement)
    if header is None or target is None:
        return set()

    table = _normalize_table(target.group(1))
    columns: set[str] = set()
    depth = 0
    index = header.end()

    while index < len(statement):
        literal_end = _opaque_end(statement, index)
        if literal_end is not None:
            index = literal_end
            continue

        char = statement[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif depth == 0:
            if _END_OF_SET_CLAUSE.match(statement, index):
                break

            assignment = _ASSIGNMENT.match(statement, index)
            if assignment is not None:
                columns.add(_normalize_identifier(assignment.group(1)))
                index = assignment.end()
                continue

        index += 1

    return {(table, column) for column in columns}


def _added_not_null_column(action: str) -> str | None:
    """Column of an ``ADD COLUMN ... NOT NULL`` with no DEFAULT, else None.

    A default makes the add metadata-only on PG11+; without one the
    constraint is checked against every existing row and the statement
    fails on any populated table.  No backfill can rescue it, because
    the column does not exist until this statement runs.
    """
    added = _ADD_COLUMN.match(action)
    if added is None:
        return None

    definition = action[added.end() :]
    if not _matches_at_top_level(_NOT_NULL, definition):
        return None
    if _matches_at_top_level(_DEFAULT, definition):
        return None

    return _normalize_identifier(added.group(1))


def find_transaction_control(sql: str) -> list[str]:
    """Report BEGIN / COMMIT / ROLLBACK / SAVEPOINT.

    The runner wraps each file in a transaction it owns.  A file that
    commits mid-way makes that wrapper decorative: everything after the
    file's own COMMIT runs unprotected, and a later failure leaves the
    schema half-migrated with the ledger row never written.

    Known hole: this also reports the BEGIN of a PL/pgSQL body.  See the
    dated deferral in the module docstring.
    """
    return _violations(sql, _TRANSACTION_CONTROL)


def find_if_not_exists(sql: str) -> list[str]:
    """Report IF NOT EXISTS and IF EXISTS alike, with no exemptions.

    Defensive DDL lies at exactly the level that matters: CREATE TABLE IF
    NOT EXISTS does not compare columns and ADD COLUMN IF NOT EXISTS does
    not compare types, so a re-run against a drifted schema reports
    success while the database stays wrong.  The ledger owns idempotency;
    a migration that also tries to own it can only disagree with it.

    IF EXISTS is the same claim from the other side.  DROP INDEX IF
    EXISTS reports success whether or not the index was there, so a
    migration written against a schema that no longer matches -- the
    index renamed, or already dropped by hand -- passes silently.  A bare
    DROP INDEX is still allowed: it names an index that must exist, and
    says so by failing when it does not.
    """
    return _violations(sql, _EXISTENCE_GUARD)


def find_on_conflict(sql: str) -> list[str]:
    """Report ON CONFLICT in seed inserts.

    Same failure as IF NOT EXISTS, one layer down: the insert reports
    success without establishing that the existing row is the row the
    migration meant to write.
    """
    return _violations(sql, _ON_CONFLICT)


def find_set_not_null_without_backfill(sql: str) -> list[str]:
    """Report NOT NULL constraints existing rows cannot satisfy.

    This is the rule that protects existing rows, and it covers both
    ways a migration can impose NOT NULL on a populated table:

    * ``ALTER COLUMN c SET NOT NULL`` with no earlier UPDATE writing c.
      SET NOT NULL is validated against the whole table, so without a
      backfill ahead of it the migration aborts on the first pre-existing
      row -- or, worse, succeeds on an empty staging database and fails
      only in production.
    * ``ADD COLUMN c ... NOT NULL`` with no DEFAULT, which fails the same
      way and cannot be backfilled at all, since the column does not
      exist until that statement runs.  With a DEFAULT it is safe, and
      allowed.

    Violations are reported table-qualified, and backfills are tracked by
    (table, column): one backfill does not license two constraints, and a
    backfill of workspaces.workspace_id says nothing about
    issues.workspace_id.
    """
    backfilled: set[tuple[str, str]] = set()
    missing: list[str] = []

    for _, statement in _iter_statements(strip_comments(sql)):
        header = _ALTER_TABLE.match(statement)

        if header is not None:
            table = _normalize_table(header.group(1))

            for _, action in _top_level_parts(statement, header.end()):
                added = _added_not_null_column(action)
                if added is not None:
                    # Unconditional: an earlier UPDATE cannot have written
                    # a column this statement is only now creating.
                    missing.append(f"{table}.{added}")
                    continue

                constrained = _SET_NOT_NULL.match(action)
                if constrained is None:
                    continue

                column = _normalize_identifier(constrained.group(1))
                if (table, column) not in backfilled:
                    missing.append(f"{table}.{column}")

        backfilled |= _assignments_by_update(statement)

    return missing


def find_destructive_statements(sql: str) -> list[str]:
    """Report DROP TABLE / DROP COLUMN / TRUNCATE / DELETE FROM.

    These discard data that no later migration can reconstruct, so they
    do not belong in an automatically applied file.  The COLUMN keyword
    is optional in Postgres, so ``ALTER TABLE issues DROP description``
    is caught too -- it destroys exactly as much as the spelled-out form.
    Sub-clauses that only alter a column (DROP DEFAULT, DROP NOT NULL,
    DROP CONSTRAINT, DROP IDENTITY, DROP EXPRESSION) are not drops of it.

    DROP INDEX is deliberately allowed: an index is derived state, and
    002 legitimately drops issues_created_at_id_idx when it widens the
    ordering key.
    """
    text = strip_comments(sql)
    found = [(match.start(), match.group()) for match in _DESTRUCTIVE.finditer(text)]

    for offset, statement in _iter_statements(text):
        header = _ALTER_TABLE.match(statement)
        if header is None:
            continue

        for action_offset, action in _top_level_parts(statement, header.end()):
            dropped = _DROP_BARE_COLUMN.match(action)
            if dropped is not None:
                found.append((offset + action_offset, dropped.group()))

    return [_report(text, index, matched) for index, matched in sorted(found)]


def read_migration(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def assert_clean_under_every_rule(sql: str) -> None:
    """All five rules, so a "this is allowed" claim is not rule-shaped."""
    assert find_transaction_control(sql) == []
    assert find_if_not_exists(sql) == []
    assert find_on_conflict(sql) == []
    assert find_set_not_null_without_backfill(sql) == []
    assert find_destructive_statements(sql) == []


# --- comment and statement handling -----------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "-- never write COMMIT or ROLLBACK in a migration\nCREATE TABLE t (id UUID);",
        "/* no IF NOT EXISTS here, and no ON CONFLICT */\nCREATE TABLE t (id UUID);",
        "CREATE TABLE t (id UUID); -- DROP TABLE is the DBA's call, not ours",
        "CREATE INDEX i ON t (id);\n/* 002 will\n   TRUNCATE nothing */",
        "-- DROP INDEX IF EXISTS is banned; plain DROP INDEX is not\n"
        "CREATE TABLE t (id UUID);",
    ],
)
def test_rule_names_inside_comments_do_not_trip_the_rules(sql):
    assert find_transaction_control(sql) == []
    assert find_if_not_exists(sql) == []
    assert find_on_conflict(sql) == []
    assert find_destructive_statements(sql) == []


def test_comment_markers_inside_string_literals_are_not_comments():
    """A '--' in seed data must not blank the rest of the line."""
    sql = "INSERT INTO notes (body) VALUES ('a -- b'); DROP TABLE issues;"

    assert find_destructive_statements(sql) == ["line 1: DROP TABLE"]


def test_violations_report_the_line_number_of_the_original_file():
    sql = "-- header\n/* two\n   lines */\nCOMMIT;"

    assert find_transaction_control(sql) == ["line 4: COMMIT"]


def test_semicolons_inside_string_literals_do_not_split_statements():
    assert len(split_statements("INSERT INTO notes (body) VALUES ('a;b');")) == 1


def test_dollar_quoted_bodies_survive_comment_stripping():
    """An apostrophe inside a function body must not open a string literal."""
    sql = "CREATE FUNCTION f() RETURNS INT AS $$ SELECT 1 /* it's fine */ $$ LANGUAGE SQL;"

    assert "SELECT 1" in strip_comments(sql)
    assert len(split_statements(sql)) == 1


# --- rule 1: no transaction control -----------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "BEGIN;\nALTER TABLE issues ADD COLUMN workspace_id UUID;",
        "ALTER TABLE issues ADD COLUMN workspace_id UUID;\nCOMMIT;",
        "ROLLBACK;",
        "SAVEPOINT before_backfill;",
        "RELEASE SAVEPOINT before_backfill;",
        "START TRANSACTION;",
        "begin;\ncommit;",
        "ALTER TABLE issues ADD COLUMN a UUID;\n\nCOMMIT;\n\nDROP INDEX i;",
    ],
)
def test_transaction_control_is_reported(sql):
    assert find_transaction_control(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE issues (id UUID PRIMARY KEY);",
        "ALTER TABLE issues ADD COLUMN committed_at TIMESTAMPTZ;",
        "ALTER TABLE issues ADD COLUMN begins_at TIMESTAMPTZ;",
        "CREATE INDEX i ON issues (id);",
        "",
    ],
)
def test_clean_sql_reports_no_transaction_control(sql):
    assert find_transaction_control(sql) == []


# --- rule 2: no existence guards --------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE IF NOT EXISTS issues (id UUID);",
        "ALTER TABLE issues ADD COLUMN IF NOT EXISTS workspace_id UUID;",
        "CREATE INDEX IF NOT EXISTS issues_workspace_idx ON issues (workspace_id);",
        # No exemption for extensions: 'already installed' is not 'installed
        # at the version and schema this migration was written against'.
        "CREATE EXTENSION IF NOT EXISTS pgcrypto;",
        "create table if not exists issues (id UUID);",
        "CREATE TABLE IF   NOT\n    EXISTS issues (id UUID);",
    ],
)
def test_if_not_exists_is_reported(sql):
    assert find_if_not_exists(sql)


@pytest.mark.parametrize(
    "sql",
    [
        # Hole #3b: DROP INDEX is allowed and rule 2 used to match only the
        # NOT form, so this defensive spelling passed every rule.
        "DROP INDEX IF EXISTS issues_created_at_id_idx;",
        "drop index if exists issues_created_at_id_idx;",
        "DROP INDEX IF\n    EXISTS issues_created_at_id_idx;",
        "DROP TABLE IF EXISTS legacy_issues;",
        "ALTER TABLE issues DROP COLUMN IF EXISTS description;",
        "ALTER TABLE IF EXISTS issues ADD COLUMN workspace_id UUID;",
    ],
)
def test_bare_if_exists_is_reported_too(sql):
    assert find_if_not_exists(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE issues (id UUID);",
        "CREATE EXTENSION pgcrypto;",
        "ALTER TABLE issues ADD CONSTRAINT c CHECK (workspace_id IS NOT NULL);",
        "DROP INDEX issues_created_at_id_idx;",
    ],
)
def test_clean_ddl_reports_no_existence_guard(sql):
    assert find_if_not_exists(sql) == []


def test_dropping_a_named_index_without_a_guard_is_clean_under_every_rule():
    """The exact statement 002 needs, asserted against all five rules.

    002 drops issues_created_at_id_idx because the ordering key widens to
    include workspace_id.  Tightening rule 2 to reject IF EXISTS must not
    catch this: it names an index that must be there, and fails loudly if
    it is not, which is the behaviour the ledger relies on.
    """
    assert_clean_under_every_rule("DROP INDEX issues_created_at_id_idx;")


# --- rule 3: no ON CONFLICT -------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO workspaces (id, name) VALUES (uuidv7(), 'Default') "
        "ON CONFLICT DO NOTHING;",
        "INSERT INTO workspaces (id) VALUES (uuidv7()) "
        "ON CONFLICT (id) DO UPDATE SET name = 'Default';",
        "insert into workspaces (id) values (uuidv7()) on conflict do nothing;",
        "INSERT INTO workspaces (id) VALUES (uuidv7())\n    ON CONFLICT DO NOTHING;",
    ],
)
def test_on_conflict_is_reported(sql):
    assert find_on_conflict(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO workspaces (id, name) VALUES (uuidv7(), 'Default');",
        "INSERT INTO workspaces (id) SELECT id FROM legacy_workspaces;",
    ],
)
def test_plain_inserts_report_no_on_conflict(sql):
    assert find_on_conflict(sql) == []


# --- rule 4: every NOT NULL is one existing rows can satisfy -----------


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        (
            "ALTER TABLE issues ADD COLUMN workspace_id UUID;\n"
            "ALTER TABLE issues ALTER COLUMN workspace_id SET NOT NULL;",
            ["issues.workspace_id"],
        ),
        (
            # Backfill lands after the constraint, so the constraint is
            # still validated against unbackfilled rows.
            "ALTER TABLE issues ALTER COLUMN workspace_id SET NOT NULL;\n"
            "UPDATE issues SET workspace_id = uuidv7();",
            ["issues.workspace_id"],
        ),
        (
            # Backfills a different column than the one being constrained.
            "UPDATE issues SET team_id = uuidv7();\n"
            "ALTER TABLE issues ALTER COLUMN workspace_id SET NOT NULL;",
            ["issues.workspace_id"],
        ),
        (
            # Two columns, one backfill: the second must still be caught.
            "UPDATE issues SET workspace_id = uuidv7();\n"
            "ALTER TABLE issues ALTER COLUMN workspace_id SET NOT NULL;\n"
            "ALTER TABLE issues ALTER COLUMN team_id SET NOT NULL;",
            ["issues.team_id"],
        ),
        (
            # The column is only read in the predicate; nothing was written.
            "UPDATE issues SET updated_at = now() WHERE workspace_id IS NULL;\n"
            "ALTER TABLE issues ALTER COLUMN workspace_id SET NOT NULL;",
            ["issues.workspace_id"],
        ),
        (
            # An assignment inside a subquery is not an assignment to the
            # table being altered.
            "UPDATE issues SET workspace_id = "
            "(SELECT id FROM workspaces WHERE team_id = 1);\n"
            "ALTER TABLE issues ALTER COLUMN team_id SET NOT NULL;",
            ["issues.team_id"],
        ),
        (
            # Both constraints in one statement, neither backfilled.
            "ALTER TABLE issues\n"
            "    ALTER COLUMN workspace_id SET NOT NULL,\n"
            "    ALTER COLUMN team_id SET NOT NULL;",
            ["issues.workspace_id", "issues.team_id"],
        ),
        (
            # The COLUMN keyword is optional in Postgres; omitting it must
            # not smuggle the constraint past the rule.
            "ALTER TABLE issues ALTER workspace_id SET NOT NULL;",
            ["issues.workspace_id"],
        ),
    ],
)
def test_set_not_null_without_a_backfill_is_reported(sql, expected):
    assert find_set_not_null_without_backfill(sql) == expected


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        (
            # Hole #1: a backfill of workspaces.workspace_id is not a
            # backfill of issues.workspace_id, however alike they read.
            "UPDATE workspaces SET workspace_id = uuidv7();\n"
            "ALTER TABLE issues ALTER COLUMN workspace_id SET NOT NULL;",
            ["issues.workspace_id"],
        ),
        (
            # The right column on the wrong table, twice over: only the
            # table that was actually backfilled is clean.
            "UPDATE workspaces SET workspace_id = uuidv7();\n"
            "ALTER TABLE workspaces ALTER COLUMN workspace_id SET NOT NULL;\n"
            "ALTER TABLE issues ALTER COLUMN workspace_id SET NOT NULL;\n"
            "ALTER TABLE teams ALTER COLUMN workspace_id SET NOT NULL;",
            ["issues.workspace_id", "teams.workspace_id"],
        ),
        (
            # A subselect in the SET clause names another table; the
            # target is still the table named after UPDATE.
            "UPDATE workspaces SET workspace_id = "
            "(SELECT id FROM issues LIMIT 1);\n"
            "ALTER TABLE issues ALTER COLUMN workspace_id SET NOT NULL;",
            ["issues.workspace_id"],
        ),
    ],
)
def test_a_backfill_on_another_table_does_not_license_the_constraint(sql, expected):
    assert find_set_not_null_without_backfill(sql) == expected


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        (
            # Hole #2: on a populated table this fails on every existing
            # row, and no backfill can precede it.
            "ALTER TABLE issues ADD COLUMN workspace_id UUID NOT NULL;",
            ["issues.workspace_id"],
        ),
        (
            "alter table issues add column workspace_id uuid not null;",
            ["issues.workspace_id"],
        ),
        (
            # The COLUMN keyword is optional here too.
            "ALTER TABLE issues ADD workspace_id UUID NOT NULL;",
            ["issues.workspace_id"],
        ),
        (
            "ALTER TABLE issues\n"
            "    ADD COLUMN workspace_id UUID NOT NULL,\n"
            "    ADD COLUMN team_id UUID NOT NULL;",
            ["issues.workspace_id", "issues.team_id"],
        ),
        (
            # One safe add, one not: the defaulted column is metadata-only,
            # the other rewrites and fails.
            "ALTER TABLE issues\n"
            "    ADD COLUMN workspace_id UUID NOT NULL DEFAULT '0'::UUID,\n"
            "    ADD COLUMN team_id UUID NOT NULL;",
            ["issues.team_id"],
        ),
        (
            # A NOT NULL foreign key is no safer than a bare one.
            "ALTER TABLE issues ADD COLUMN workspace_id UUID NOT NULL "
            "REFERENCES workspaces (id);",
            ["issues.workspace_id"],
        ),
        (
            # An earlier UPDATE cannot have written a column this statement
            # is only now creating, so it must not excuse the constraint.
            "UPDATE issues SET workspace_id = uuidv7();\n"
            "ALTER TABLE issues ADD COLUMN workspace_id UUID NOT NULL;",
            ["issues.workspace_id"],
        ),
    ],
)
def test_add_column_not_null_without_a_default_is_reported(sql, expected):
    assert find_set_not_null_without_backfill(sql) == expected


@pytest.mark.parametrize(
    "sql",
    [
        "ALTER TABLE issues ADD COLUMN workspace_id UUID;\n"
        "UPDATE issues SET workspace_id = uuidv7();\n"
        "ALTER TABLE issues ALTER COLUMN workspace_id SET NOT NULL;",
        # Two columns, both backfilled by one UPDATE.
        "UPDATE issues SET workspace_id = uuidv7(), team_id = uuidv7();\n"
        "ALTER TABLE issues ALTER COLUMN workspace_id SET NOT NULL;\n"
        "ALTER TABLE issues ALTER COLUMN team_id SET NOT NULL;",
        # Two columns, two backfills.
        "UPDATE issues SET workspace_id = uuidv7();\n"
        "UPDATE issues SET team_id = uuidv7();\n"
        "ALTER TABLE issues\n"
        "    ALTER COLUMN workspace_id SET NOT NULL,\n"
        "    ALTER COLUMN team_id SET NOT NULL;",
        # A guarded backfill still writes the column.
        "UPDATE issues SET workspace_id = uuidv7() WHERE workspace_id IS NULL;\n"
        "ALTER TABLE issues ALTER COLUMN workspace_id SET NOT NULL;",
        # Quoted on one side, bare on the other: the same column.
        'UPDATE issues SET "workspace_id" = uuidv7();\n'
        "ALTER TABLE issues ALTER COLUMN workspace_id SET NOT NULL;",
        # A backfill whose value comes from a subquery.
        "UPDATE issues SET workspace_id = "
        "(SELECT id FROM workspaces ORDER BY created_at LIMIT 1);\n"
        "ALTER TABLE issues ALTER COLUMN workspace_id SET NOT NULL;",
        # Nothing to check.
        "CREATE TABLE teams (id UUID PRIMARY KEY, name TEXT NOT NULL);",
    ],
)
def test_backfilled_columns_report_no_violation(sql):
    assert find_set_not_null_without_backfill(sql) == []


@pytest.mark.parametrize(
    "sql",
    [
        # The near-miss of hole #1: same column name, and every table that
        # constrains it was backfilled on its own.
        "UPDATE workspaces SET workspace_id = uuidv7();\n"
        "UPDATE issues SET workspace_id = uuidv7();\n"
        "ALTER TABLE workspaces ALTER COLUMN workspace_id SET NOT NULL;\n"
        "ALTER TABLE issues ALTER COLUMN workspace_id SET NOT NULL;",
        # 002's seeding shape: the SET clause reads other tables, but the
        # target is the table named after UPDATE.
        "UPDATE issues SET workspace_id = "
        "(SELECT id FROM workspaces WHERE slug = 'vector'), "
        "team_id = (SELECT id FROM teams WHERE slug = 'core') "
        "WHERE workspace_id IS NULL;\n"
        "ALTER TABLE issues ALTER COLUMN workspace_id SET NOT NULL;\n"
        "ALTER TABLE issues ALTER COLUMN team_id SET NOT NULL;",
        # A schema prefix names the same table as the bare form.
        "UPDATE public.issues SET workspace_id = uuidv7();\n"
        "ALTER TABLE issues ALTER COLUMN workspace_id SET NOT NULL;",
        # So does a quoted one.
        'UPDATE "issues" SET workspace_id = uuidv7();\n'
        "ALTER TABLE issues ALTER COLUMN workspace_id SET NOT NULL;",
    ],
)
def test_per_table_backfills_report_no_violation(sql):
    assert find_set_not_null_without_backfill(sql) == []


@pytest.mark.parametrize(
    "sql",
    [
        # The near-miss of hole #2: a default makes the add metadata-only
        # on PG11+, so existing rows are never rewritten.
        "ALTER TABLE issues ADD COLUMN workspace_id UUID NOT NULL "
        "DEFAULT '00000000-0000-0000-0000-000000000000'::UUID;",
        "ALTER TABLE issues ADD COLUMN priority SMALLINT NOT NULL DEFAULT 0;",
        "ALTER TABLE issues ADD COLUMN created_at TIMESTAMPTZ NOT NULL DEFAULT now();",
        "alter table issues add column priority smallint not null default 0;",
        # Nullable now, constrained later once the backfill has run.
        "ALTER TABLE issues ADD COLUMN workspace_id UUID;",
        # NOT NULL nested in a constraint is not a column definition.
        "ALTER TABLE issues ADD CONSTRAINT issues_workspace_present "
        "CHECK (workspace_id IS NOT NULL);",
        # A NOT NULL column in a brand-new table has no existing rows.
        "CREATE TABLE workspaces (id UUID PRIMARY KEY, name TEXT NOT NULL);",
        # A comma inside the type is not the start of a second action.
        "ALTER TABLE issues ADD COLUMN cost NUMERIC(10, 2) NOT NULL DEFAULT 0;",
    ],
)
def test_safe_add_column_reports_no_violation(sql):
    assert find_set_not_null_without_backfill(sql) == []


# --- rule 5: no unguarded destructive statements ----------------------


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE issues;",
        "TRUNCATE issues;",
        "TRUNCATE TABLE issues RESTART IDENTITY;",
        "ALTER TABLE issues DROP COLUMN description;",
        "DELETE FROM issues;",
        "delete from issues where completed_at is null;",
        "DROP TABLE\n    issues;",
    ],
)
def test_destructive_statements_are_reported(sql):
    assert find_destructive_statements(sql)


@pytest.mark.parametrize(
    "sql",
    [
        # Hole #3a: the COLUMN keyword is optional in Postgres, and the
        # column is just as gone without it.
        "ALTER TABLE issues DROP description;",
        "alter table issues drop description;",
        "ALTER TABLE issues DROP\n    description;",
        # Quoting either name must not end the match early.
        'ALTER TABLE issues DROP "description";',
        'ALTER TABLE "issues" DROP "description";',
        "ALTER TABLE public.issues DROP description;",
        "ALTER TABLE issues DROP description CASCADE;",
        # Mixed with a legitimate action: the drop must still surface.
        "ALTER TABLE issues DROP CONSTRAINT issues_priority_range, "
        "DROP description;",
    ],
)
def test_dropping_a_column_without_the_keyword_is_reported(sql):
    assert find_destructive_statements(sql)


def test_a_bare_column_drop_names_the_column_it_discards():
    sql = "ALTER TABLE issues\n    DROP description;"

    assert find_destructive_statements(sql) == ["line 2: DROP description"]


def test_a_spelled_out_column_drop_is_reported_once():
    """DROP COLUMN must not be counted by both scanners."""
    assert find_destructive_statements("ALTER TABLE issues DROP COLUMN description;") == [
        "line 1: DROP COLUMN"
    ]


@pytest.mark.parametrize(
    "sql",
    [
        # Indexes are derived state; 002 drops issues_created_at_id_idx.
        "DROP INDEX issues_created_at_id_idx;",
        "ALTER TABLE issues ADD COLUMN deleted_at TIMESTAMPTZ;",
        "ALTER TABLE issues ALTER COLUMN priority DROP DEFAULT;",
        "ALTER TABLE issues DROP CONSTRAINT issues_priority_range;",
    ],
)
def test_non_destructive_statements_report_nothing(sql):
    assert find_destructive_statements(sql) == []


@pytest.mark.parametrize(
    "sql",
    [
        # The near-misses of hole #3a: every DROP here alters a column or
        # a constraint rather than discarding data.
        "ALTER TABLE issues ALTER COLUMN workspace_id DROP NOT NULL;",
        "ALTER TABLE issues ALTER COLUMN priority DROP DEFAULT;",
        "ALTER TABLE issues ALTER COLUMN id DROP IDENTITY;",
        "ALTER TABLE issues ALTER COLUMN slug DROP EXPRESSION;",
        "alter table issues drop constraint issues_priority_range;",
        "ALTER TABLE issues\n    DROP CONSTRAINT issues_priority_range,\n"
        "    ADD CONSTRAINT issues_priority_range CHECK (priority BETWEEN 0 AND 9);",
    ],
)
def test_column_altering_drops_report_nothing(sql):
    assert find_destructive_statements(sql) == []


# --- hole #4: PL/pgSQL bodies, deliberately deferred ------------------


_FUNCTION_BODY = (
    "CREATE FUNCTION touch_updated_at() RETURNS TRIGGER AS $$\n"
    "BEGIN\n"
    "    NEW.updated_at = now();\n"
    "    RETURN NEW;\n"
    "END\n"
    "$$ LANGUAGE plpgsql;"
)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Hole #4, deferred 2026-09-02: rule 1 does not exempt dollar-quoted "
        "regions, so the BEGIN opening a PL/pgSQL block reads as transaction "
        "control.  Safe today because no migration in the repo has a function "
        "body; the updated_at trigger lands in 003.  When this turns green, "
        "delete it and the deferral note in the module docstring."
    ),
)
def test_a_plpgsql_function_body_does_not_trip_rule_1():
    assert find_transaction_control(_FUNCTION_BODY) == []


def test_a_commit_outside_a_function_body_is_still_reported():
    """The test that must survive the eventual fix to hole #4.

    Exempting dollar-quoted regions has to skip the body, not the file:
    a COMMIT after the function is exactly as fatal as one before it.
    """
    assert "line 7: COMMIT" in find_transaction_control(_FUNCTION_BODY + "\nCOMMIT;")


# --- the real files ---------------------------------------------------


def test_the_linter_can_see_the_real_migrations():
    """Every file test below is parametrized over this list.

    An empty glob -- a moved directory, a renamed extension -- would turn
    all of them green while checking nothing at all.
    """
    assert "001_issues.sql" in {path.name for path in MIGRATION_FILES}


@pytest.mark.parametrize("path", MIGRATION_FILES, ids=lambda path: path.name)
def test_migration_has_no_transaction_control(path):
    assert find_transaction_control(read_migration(path)) == []


@pytest.mark.parametrize("path", MIGRATION_FILES, ids=lambda path: path.name)
def test_migration_has_no_if_not_exists(path):
    assert find_if_not_exists(read_migration(path)) == []


@pytest.mark.parametrize("path", MIGRATION_FILES, ids=lambda path: path.name)
def test_migration_has_no_on_conflict(path):
    assert find_on_conflict(read_migration(path)) == []


@pytest.mark.parametrize("path", MIGRATION_FILES, ids=lambda path: path.name)
def test_migration_backfills_before_every_set_not_null(path):
    assert find_set_not_null_without_backfill(read_migration(path)) == []


@pytest.mark.parametrize("path", MIGRATION_FILES, ids=lambda path: path.name)
def test_migration_has_no_destructive_statements(path):
    assert find_destructive_statements(read_migration(path)) == []


# --- the shape 002 is expected to have --------------------------------


_PLANNED_002 = """
-- Tenancy: workspaces and teams, with issues backfilled into the
-- default workspace before either column is made NOT NULL.

CREATE TABLE workspaces (
    id UUID PRIMARY KEY DEFAULT uuidv7(),
    slug TEXT NOT NULL,
    name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE teams (
    id UUID PRIMARY KEY DEFAULT uuidv7(),
    workspace_id UUID NOT NULL REFERENCES workspaces (id),
    slug TEXT NOT NULL,
    name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO workspaces (slug, name)
    SELECT 'vector', 'Vector';

INSERT INTO teams (workspace_id, slug, name)
    SELECT id, 'core', 'Core' FROM workspaces WHERE slug = 'vector';

ALTER TABLE issues ADD COLUMN workspace_id UUID;
ALTER TABLE issues ADD COLUMN team_id UUID;

UPDATE issues
   SET workspace_id = (SELECT id FROM workspaces WHERE slug = 'vector'),
       team_id = (SELECT id FROM teams WHERE slug = 'core')
 WHERE workspace_id IS NULL;

ALTER TABLE issues ALTER COLUMN workspace_id SET NOT NULL;
ALTER TABLE issues ALTER COLUMN team_id SET NOT NULL;

DROP INDEX issues_created_at_id_idx;

CREATE INDEX issues_workspace_created_at_id_idx
    ON issues (workspace_id, created_at DESC, id DESC);
"""


def test_the_planned_002_passes_every_rule():
    """The linter must not reject the migration it exists to protect.

    Rules tightened against holes #1-#3 all touch statements this file
    contains -- a cross-table UPDATE with subselects in its SET clause,
    two ADD COLUMNs, a bare DROP INDEX -- so a fix that over-fires would
    show up here rather than the day 002 is written.
    """
    assert_clean_under_every_rule(_PLANNED_002)
