"""Every statement in the repository layer is scoped to one tenant.

Tenancy in this application is a `workspace_id` equality in a WHERE clause and
a composite foreign key through the same column (migrations/002_tenancy.sql).
There is no row-level security, no session GUC and no ORM filter -- so a
statement that omits the predicate is not degraded, it is unscoped, and it
reads or writes another tenant's rows for anybody who can name an id.

Every other tenancy test in this repository asks the question about a
particular field: `tests/test_graphql_workspace_scope_db.py` for issues,
labels, cycles and projects, `tests/test_graphql_tenancy_surface_db.py` for
the six entities that landed after it. Those are the right tests and they are
end-to-end, but they are also a list that somebody has to remember to extend.
The next repository method to forget the predicate will be one nothing in
that list covers, and it will be green everywhere until somebody guesses to
look.

This asks the question about the LAYER instead, and it asks it without a
database: every SQL statement written in `app/repositories/` either carries
`workspace_id`, or names only relations that have no tenant column at all.
The second branch is a short, explicit list -- adding to it is the review
moment this file exists to create.

A structural gate rather than a behavioural one, so it runs in the default
suite in milliseconds and fails at the moment the statement is written rather
than at the moment somebody thinks to attack it.
"""

import ast
import pathlib
import re

import pytest


REPOSITORIES = pathlib.Path(__file__).resolve().parents[1] / "app" / "repositories"

# What counts as a statement: a string that BEGINS one. A fragment -- a
# column list, a JOIN clause, a predicate pulled out into a constant -- is
# not checked here, because it is meaningless on its own and is checked as
# part of whichever statement interpolates it.
STATEMENT = re.compile(r"^(SELECT|INSERT|UPDATE|DELETE|WITH)\s", re.IGNORECASE)

# asyncpg's command tag, which every `execute` returns and three repositories
# compare against: "DELETE 1", "DELETE 0", "INSERT 0 1". They begin with a SQL
# verb and are not SQL, so they are excluded by shape rather than by being
# listed -- a tag is a verb followed only by digits.
COMMAND_TAG = re.compile(r"^(SELECT|INSERT|UPDATE|DELETE)(\s+\d+)+$", re.IGNORECASE)

# The relations a statement names. Deliberately only the four keywords that
# introduce one, and the trailing lookahead excludes a set-returning FUNCTION
# -- `FROM unnest($1::text[], $2::text[])` names no table, and reading it as
# one would put "unnest" in the exemption list beside the real tables.
RELATION = re.compile(
    r"\b(?:FROM|INTO|JOIN|UPDATE)\s+(?:ONLY\s+|LATERAL\s+)?([a-z_][a-z0-9_]*)(?!\s*\()",
    re.IGNORECASE,
)

# Words the pattern above can capture that are not names of anything. `SET` is
# the one that actually occurs: an upsert's `ON CONFLICT ... DO UPDATE SET`
# puts it exactly where a table name would be.
NOT_A_RELATION = frozenset({"set", "select", "values", "only", "lateral"})


def _relations(sql: str) -> set[str]:
    return {match.lower() for match in RELATION.findall(sql)} - NOT_A_RELATION

# The predicate itself, in any spelling a statement may use for it --
# `workspace_id = $1`, `issues.workspace_id = $1`, or the join that carries it
# from one table to another.
TENANT_COLUMN = "workspace_id"

# Tables that have no tenant column, because the thing they hold does not
# belong to a workspace. Each one is a decision, not an oversight:
#
#   auth_rate_limits       counted BEFORE anybody is authenticated, so there is
#                          no workspace to key on -- migration 032. Its subject
#                          is an address digest or an IP.
#   github_deliveries      GitHub's delivery id, deduplicated globally because
#                          one delivery is one delivery whichever workspace it
#                          turns out to concern -- migration 013.
#   sessions               a session authenticates an ACCOUNT. One person is a
#                          member of several workspaces through one session,
#                          so a tenant column here would be wrong rather than
#                          missing -- migration 003.
#   slack_event_deliveries the same argument as github_deliveries, for
#                          migration 014's event ids.
#   users                  an account is global, for the reason `sessions` is.
#                          Which workspaces it may act in lives in
#                          `workspace_members`, and that is the only table that
#                          answers the question.
#   workspaces             the table that DEFINES the tenant. Scoping it to
#                          itself is the one place the predicate cannot apply;
#                          the two statements against it resolve a slug and
#                          insert a row, and neither is reachable without the
#                          membership check that follows it.
#
# A statement naming NO relation at all -- `SELECT pg_advisory_xact_lock($1,
# hashtext($2))`, the three cycle guards -- passes too, and safely: it reads no
# table, so there is nothing of another tenant's for it to return. The lock key
# is derived from the caller's own workspace by the service that takes it.
TENANT_FREE_RELATIONS = frozenset(
    {
        "auth_rate_limits",
        "github_deliveries",
        "sessions",
        "slack_event_deliveries",
        "users",
        "workspaces",
    }
)

# How many statements this layer holds, as a floor rather than an equality.
#
# The gate's one failure mode is finding nothing: a collector that stopped
# recognising statements -- because somebody moved SQL into a helper, or into
# a shape the walk does not render -- would report a clean layer with no
# statements in it and pass forever. An equality would break on every new
# method; a floor breaks only if the collector goes blind, which is the case
# worth being told about. It was 313 when this was written.
MINIMUM_STATEMENTS = 280


def _docstrings(tree: ast.Module) -> set[int]:
    """The id() of every docstring node, so prose is not read as SQL.

    Several methods here open with "Delete the document, reporting whether
    there was one to delete" -- which begins with a SQL verb and is a
    sentence.
    """
    found = set()

    for node in ast.walk(tree):
        if not isinstance(
            node,
            ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
        ):
            continue

        first = node.body[0] if node.body else None

        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            found.add(id(first.value))

    return found


def _interpolated_parts(tree: ast.Module) -> set[int]:
    """The id() of every literal chunk inside an f-string.

    `ast.walk` yields a JoinedStr AND the Constants it is built from, so
    without this the leading `"SELECT\\n"` of `f"SELECT {COLUMNS} FROM ..."`
    is collected as a statement of its own -- one that names no relation and
    carries no predicate, and would be waved through while the real statement
    went unread.
    """
    return {
        id(part)
        for node in ast.walk(tree)
        if isinstance(node, ast.JoinedStr)
        for part in node.values
    }


def _render(node: ast.Constant | ast.JoinedStr) -> str:
    """One SQL literal, whitespace-collapsed, interpolations left as `{}`.

    What an interpolation renders to does not matter here. Every one of them
    in this layer is a module-level constant or a `$n` placeholder computed
    from a bind index -- verified by reading all 83 of them -- and none can
    introduce or remove a WHERE clause, because the fragments they name are
    themselves collected and checked wherever they begin a statement.
    """
    if isinstance(node, ast.Constant):
        text = node.value
    else:
        text = "".join(
            part.value if isinstance(part, ast.Constant) else "{}"
            for part in node.values
        )

    return " ".join(text.split())


def _statements(path: pathlib.Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip = _docstrings(tree) | _interpolated_parts(tree)
    found = []

    for node in ast.walk(tree):
        if id(node) in skip:
            continue

        if isinstance(node, ast.Constant):
            if not isinstance(node.value, str):
                continue
        elif not isinstance(node, ast.JoinedStr):
            continue

        rendered = _render(node)

        if STATEMENT.match(rendered) and not COMMAND_TAG.match(rendered):
            found.append((node.lineno, rendered))

    return found


REPOSITORY_FILES = sorted(REPOSITORIES.glob("*.py"))

ALL_STATEMENTS = [
    (path, lineno, sql)
    for path in REPOSITORY_FILES
    for lineno, sql in _statements(path)
]


def test_the_collector_found_the_layer():
    """The guard on the guard.

    Every assertion below is over a list this module builds by reading the
    source, so a collector that quietly stopped finding statements would make
    the whole file pass while checking nothing. Both halves are asserted: that
    there are files, and that they hold roughly the number of statements this
    layer is known to have.
    """
    assert len(REPOSITORY_FILES) > 20
    assert len(ALL_STATEMENTS) >= MINIMUM_STATEMENTS


@pytest.mark.parametrize(
    ("path", "lineno", "sql"),
    ALL_STATEMENTS,
    ids=[f"{path.stem}:{lineno}" for path, lineno, _ in ALL_STATEMENTS],
)
def test_every_statement_is_scoped_to_a_workspace(
    path: pathlib.Path, lineno: int, sql: str
):
    """A statement carries the tenant predicate, or names no tenant-owned table.

    THE FAILURE THIS EXISTS FOR is a new repository method whose WHERE clause
    is `id = $1`. Nothing about that fails to compile, nothing about it fails
    a type check, and every existing test goes on passing -- the method works
    perfectly for the caller who wrote it, and works just as well for a caller
    who guessed somebody else's id.

    Two ways to satisfy it, and the second is the one to think about. If the
    statement really does address a table with no tenant column, add that
    table to TENANT_FREE_RELATIONS with the reason -- which is the review this
    file is here to force. If it addresses a tenant-owned table, the predicate
    is missing and this is the bug report.

    Matching on the column NAME rather than on a parsed WHERE clause is
    deliberate: parsing SQL to decide whether a predicate is load-bearing
    means a SQL parser in the test suite, and the weaker check catches the
    only mistake anyone actually makes, which is leaving the column out
    entirely.
    """
    if TENANT_COLUMN in sql:
        return

    unscoped = _relations(sql) - TENANT_FREE_RELATIONS

    assert not unscoped, (
        f"{path.name}:{lineno} touches {', '.join(sorted(unscoped))} without a "
        f"{TENANT_COLUMN} predicate. Either the statement is missing its "
        f"tenancy predicate, or the table has no tenant column and belongs in "
        f"TENANT_FREE_RELATIONS with the reason written down.\n\n  {sql[:400]}"
    )


def test_the_tenant_free_list_is_still_earned():
    """No entry survives that nothing uses any more.

    A list of exemptions that only grows is a list that stops being read. If a
    table here is no longer addressed by any unscoped statement -- it gained a
    tenant column, or the statements went away -- the entry has to go too, so
    that what is left is exactly what somebody decided.
    """
    claimed = {
        relation
        for _path, _lineno, sql in ALL_STATEMENTS
        if TENANT_COLUMN not in sql
        for relation in _relations(sql)
    }

    assert claimed <= TENANT_FREE_RELATIONS
    assert TENANT_FREE_RELATIONS - claimed == set()
