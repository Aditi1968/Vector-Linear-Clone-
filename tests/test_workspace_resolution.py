"""Slug to WorkspaceScope, asked at four levels of the same question.

Workspace resolution is the first thing every tenant-owned operation will do,
and the only place where a mistake in it is still cheap. Three of its failure
modes are silent by construction, and each is caught at a different level:

  * a scope that carries more than an identity, or that can be built without
    one, makes "which tenant is this" a question with a default answer.
    Section A pins the dataclass contract, including the fields it
    deliberately does not have.
  * a lookup that folds case, pattern-matches, or writes its argument into the
    SQL turns one tenant's slug into another tenant's rows. Section B reads
    the statement the repository actually sends.
  * a service that answers an unknown slug with the bootstrap workspace routes
    every unrecognised request into the one tenant that already holds data.
    Section C proves it raises instead, and that it hands the repository the
    caller's slug rather than a normalised one.

None of A, B or C reaches a database, so none of them is evidence about
PostgreSQL: a fake connection agrees with whatever SQL it is handed. Section D
builds the schema through the real migration runner -- 001, then 002 -- and
asks a real PostgreSQL 18 the same questions, with a *second* workspace
present so that "returns the right id" cannot be satisfied by returning the
only row, nor by returning the first one.

Section D is marked `db`: deselected by default, skipped when Docker is
unreachable. Nothing in this file touches DATABASE_URL or Neon; the only
server it speaks to is the throwaway container `postgres_dsn` starts.
"""

import asyncio
from dataclasses import FrozenInstanceError, dataclass, fields
from pathlib import Path
from typing import get_type_hints
from uuid import UUID

import asyncpg
import pytest

from app.domain.errors import WorkspaceNotFoundError
from app.domain.tenancy import WorkspaceScope
from app.repositories.workspaces import WorkspaceRepository
from app.services.workspaces import WorkspaceService
from scripts.apply_migration import apply_migration, read_migration

from tests.conftest import FakeConnection, FakePool, normalize


MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"
MIGRATION_001 = MIGRATIONS_DIR / "001_issues.sql"
MIGRATION_002 = MIGRATIONS_DIR / "002_tenancy.sql"

# The tenant migrations/002_tenancy.sql seeds, written as literals rather than
# read back out of the database. 002 names both in the file precisely so that a
# test can assert against a constant instead of querying for the value it is
# about to check.
BOOTSTRAP_SLUG = "vector"
BOOTSTRAP_WORKSPACE_ID = UUID("00000000-0000-7000-8000-000000000001")

# A second tenant, created by this file rather than by the migration, and the
# reason section D can say anything at all about *which* workspace resolved.
#
# Both of the orderings an accidental "just take a row" implementation would
# fall into are covered by the pair, which is why the id and the insertion
# order are chosen rather than arbitrary. `...00a1` sorts after `...0001`, and
# the row is inserted after the migration's, so ascending id, ascending
# created_at and ascending ctid all put the bootstrap workspace first, while
# ascending slug ('acme' < 'vector') and every descending order put this one
# first. A `LIMIT 1` with any ORDER BY therefore returns the same workspace for
# both slugs, and one of the two lookups in section D contradicts it.
SECOND_SLUG = "acme"
SECOND_WORKSPACE_ID = UUID("00000000-0000-7000-8000-0000000000a1")

# The whole statement WorkspaceRepository.find_id_by_slug sends, normalized.
LOOKUP_SQL = "SELECT id FROM workspaces WHERE slug = $1"

# Slugs shaped like an attempt on the query. None of them is expected to do
# anything -- see the honesty note on the section B tripwire about what a fake
# connection can and cannot establish -- but they are the strings whose
# treatment has to be identical to any other string's.
INJECTION_SLUGS = (
    "x' OR TRUE --",
    "' OR 1=1 --",
    "vector' --",
    "'; DROP TABLE workspaces; --",
    "vector'; UPDATE workspaces SET slug = 'pwned'; --",
)

# Slugs that must miss against a populated database. The first four are
# ordinary near-misses; the rest are LIKE metacharacters, which resolve to
# nothing only because the comparison is `=`. Under a `LIKE` or `SIMILAR TO`
# the bare `%` matches every workspace in the table and a client that sent it
# would be handed whichever tenant came back first.
ABSENT_SLUGS = (
    "no-such-workspace",
    "acm",
    "acmee",
    "vecto",
    "",
    "%",
    "_",
    "acm%",
    "%cme",
    "_cme",
)

# Capitalisations of two slugs that do exist in lowercase. These resolve to
# nothing rather than to their lowercase twin, and the reason is structural:
# `workspaces_slug_format` in migrations/002_tenancy.sql confines every stored
# slug to lowercase, so no row a capitalised slug could match is storable in
# the first place. That the constraint is present and bites is pinned by
# test_migration_002_db.py; what is pinned here is that resolution does not
# reintroduce the folding the constraint exists to make impossible.
CAPITALISED_SLUGS = ("Vector", "VECTOR", "vEcToR", "Acme", "ACME")

# Row identity and row content in one snapshot. `xmin` is the transaction that
# last wrote the tuple and `ctid` is where it physically sits, so either one
# changes if a row is updated -- including an update that writes back the same
# values, which a comparison of the visible columns alone would not notice.
WORKSPACE_SNAPSHOT_SQL = """
SELECT xmin::text AS xmin, ctid::text AS ctid, id, slug, name, created_at
FROM workspaces
ORDER BY id
"""

TEAM_SNAPSHOT_SQL = """
SELECT xmin::text AS xmin, ctid::text AS ctid, id, workspace_id, name, created_at
FROM teams
ORDER BY id
"""

INSERT_WORKSPACE_SQL = "INSERT INTO workspaces (id, slug, name) VALUES ($1, $2, $3)"

# Matches the rest of the db suite (tests/test_issue_pagination_db.py). The
# size is not load-bearing -- see the `resolved` fixture on why a leaked
# connection is checked for rather than provoked by starving the pool.
POOL_MAX_SIZE = 2

# A ceiling on the repeated-lookup sequence, not a performance budget. The
# sequence is twenty-odd indexed single-row lookups against a two-row table
# and finishes in well under a second on the container this runs in, so
# thirty seconds cannot be reached by a slow machine -- only by an await that
# is never going to return.
LOOKUP_SEQUENCE_TIMEOUT = 30.0


# --------------------------------------------------------------------------
# A. The domain type
# --------------------------------------------------------------------------


def test_a_scope_holds_the_workspace_id_it_was_constructed_with():
    """The whole of what a scope is: one id, kept as it was given.

    The identity check matters more than the equality one. A scope that
    reconstructed, parsed or canonicalised its argument would be doing work
    nobody asked it to do at the exact point where tenant identity is decided.
    """
    workspace_id = UUID("00000000-0000-7000-8000-00000000f00d")

    scope = WorkspaceScope(workspace_id=workspace_id)

    assert scope.workspace_id is workspace_id
    assert isinstance(scope.workspace_id, UUID)


def test_a_scope_cannot_be_reassigned():
    """Frozen, because a mutable scope is a tenant that can change mid-request.

    The value travels through service and repository calls. If any frame down
    that chain could rebind `workspace_id`, the caller that resolved the
    tenant and the code that reads it would no longer be talking about the
    same workspace, and nothing at the call site would show it.
    """
    scope = WorkspaceScope(workspace_id=BOOTSTRAP_WORKSPACE_ID)

    with pytest.raises(FrozenInstanceError):
        scope.workspace_id = SECOND_WORKSPACE_ID

    assert scope.workspace_id == BOOTSTRAP_WORKSPACE_ID


def test_a_scope_has_no_instance_dictionary():
    """`slots=True` is asserted on the object, not read off the decorator.

    Without slots a scope accepts any attribute at all, so `scope.role`,
    `scope.user_id` or a misspelling of `workspace_id` would each attach
    silently and read back as though the type had always had it. That is
    precisely how a type documented as "an identity, not a permission" grows
    into something a reader treats as a permission.

    Asserted as the absence of a `__dict__` plus the exact `__slots__`, which
    together leave nowhere for an extra attribute to be stored. The direct
    version -- assign `scope.role` and expect an AttributeError -- was written
    first and removed: on CPython 3.12 a frozen *and* slotted dataclass raises
    `TypeError: super(type, obj): obj must be an instance or subtype of type`
    instead, because `slots=True` builds a replacement class while the frozen
    `__setattr__` still closes over the original one. That is an interpreter
    detail rather than anything this type promises, and pinning it would be a
    test of CPython.
    """
    scope = WorkspaceScope(workspace_id=BOOTSTRAP_WORKSPACE_ID)

    assert not hasattr(scope, "__dict__")
    assert WorkspaceScope.__slots__ == ("workspace_id",)


def test_a_scope_carries_exactly_one_declared_field():
    """Equality both ways, so an unasked-for field is a failure.

    A subset check would wave through every addition considered and left out.
    Resolved through `get_type_hints` rather than `field.type` because the
    latter is whatever was written in the annotation -- a string under
    deferred evaluation -- and the claim here is about the type, not its
    spelling.
    """
    scope = WorkspaceScope(workspace_id=BOOTSTRAP_WORKSPACE_ID)

    assert [field.name for field in fields(WorkspaceScope)] == ["workspace_id"]
    assert get_type_hints(WorkspaceScope) == {"workspace_id": UUID}

    # Named individually rather than left to the equality above, because these
    # two are the specific additions that would turn an identity into a claim
    # about a caller. Holding a scope would start to read like "this caller
    # was authenticated and is allowed here", which nothing has established.
    assert not hasattr(scope, "user_id")
    assert not hasattr(scope, "role")


def test_a_scope_cannot_be_built_without_a_workspace():
    """No default argument, which is what "no default workspace" means here.

    A `workspace_id` with a default would make `WorkspaceScope()` valid, and
    the only sensible value for that default is the bootstrap workspace -- the
    one tenant that already holds every issue in the database. Every caller
    that forgot to resolve a slug would then quietly address it.
    """
    with pytest.raises(TypeError):
        WorkspaceScope()


def test_scopes_compare_and_hash_by_workspace_id():
    """Value semantics, which is the reason `frozen` rather than plain slots.

    Two scopes for the same workspace have to be interchangeable -- comparable
    and usable as a key -- or every test and cache that holds one starts
    depending on which frame constructed it. The last assertion is the other
    half: a scope is not its id, so a comparison that accidentally reaches
    across the two types is false rather than true.
    """
    one = WorkspaceScope(workspace_id=BOOTSTRAP_WORKSPACE_ID)
    same = WorkspaceScope(workspace_id=BOOTSTRAP_WORKSPACE_ID)
    other = WorkspaceScope(workspace_id=SECOND_WORKSPACE_ID)

    assert one == same
    assert one != other
    assert hash(one) == hash(same)
    assert len({one, same, other}) == 2

    assert one != BOOTSTRAP_WORKSPACE_ID


# --------------------------------------------------------------------------
# B. The statement the repository sends
# --------------------------------------------------------------------------


async def test_find_id_by_slug_selects_the_id_from_workspaces_by_equality():
    """The four clauses that make this a slug lookup and not something else.

    Asserted separately from the whole-statement pin further down so that a
    failure localises: this one says which clause changed, that one says the
    statement grew something it did not have.
    """
    connection = FakeConnection(row={"id": BOOTSTRAP_WORKSPACE_ID})

    await WorkspaceRepository().find_id_by_slug(connection, BOOTSTRAP_SLUG)

    query = normalize(connection.queries[0]["query"])

    assert "SELECT id" in query
    assert "FROM workspaces" in query
    assert "WHERE slug = $1" in query
    assert connection.queries[0]["args"] == (BOOTSTRAP_SLUG,)


async def test_the_lookup_statement_is_exactly_one_equality_and_nothing_more():
    """Pinned whole, because the value of this query is in what it omits.

    Every clause absent here is a decision the repository's docstring argues
    for, and each of them is a plausible-looking addition:

      * `LOWER(slug) = LOWER($1)` would make tenants addressable by any
        capitalisation, which is exactly what `workspaces_slug_format` exists
        to prevent -- and the constraint would go on passing, because the
        stored side would still be lowercase;
      * `LIKE $1` or `SIMILAR TO $1` would hand a client that sent `%` the
        first workspace in the table;
      * `LIMIT 1` would claim doubt about `workspaces_slug_key`, and would
        turn a duplicate-slug bug from an error into an arbitrary answer;
      * an `ORDER BY`, an `OR`, a `UNION` or a join would each be a second way
        for a row other than the matching one to come back.

    A substring test for any single clause passes while the others are added.
    Comparing the normalized statement is what makes an addition a failure.
    """
    connection = FakeConnection(row={"id": BOOTSTRAP_WORKSPACE_ID})

    await WorkspaceRepository().find_id_by_slug(connection, BOOTSTRAP_SLUG)

    assert normalize(connection.queries[0]["query"]) == LOOKUP_SQL


async def test_find_id_by_slug_returns_the_id_of_a_matching_row():
    """The id comes out as a UUID, not as the row it arrived in.

    `asyncpg.Record` must not escape the repository, and the fake row here is
    a dict standing in for one: what is asserted is that the return value is
    the id itself and nothing wrapping it.
    """
    connection = FakeConnection(row={"id": SECOND_WORKSPACE_ID})

    found = await WorkspaceRepository().find_id_by_slug(connection, SECOND_SLUG)

    assert found == SECOND_WORKSPACE_ID
    assert isinstance(found, UUID)


async def test_find_id_by_slug_returns_none_when_nothing_matches():
    """A miss is an ordinary answer here; only the service decides what it means.

    The query count is asserted alongside it. A repository that answered a
    miss by looking again -- lowercased, or for a default workspace -- would
    still return None from *this* fake and would still pass a return-value
    check on its own.
    """
    connection = FakeConnection(row=None)

    found = await WorkspaceRepository().find_id_by_slug(connection, "no-such-workspace")

    assert found is None
    assert len(connection.queries) == 1


@pytest.mark.parametrize("slug", INJECTION_SLUGS)
async def test_an_injection_shaped_slug_stays_a_bound_value(slug):
    """A SQL-shape tripwire, and honestly not more than that.

    A fake connection records whatever text it is handed and never parses it,
    so nothing here establishes how PostgreSQL would treat these strings. What
    it does establish is the property that makes the question moot: the slug
    reaches the driver as a separate bound parameter and no fragment of it
    appears in the statement, so there is no string for a server to reparse.
    Two independent mistakes are caught -- building the SQL with an f-string,
    and building it with `%` formatting -- both of which produce a statement
    containing the slug and no `$1`.

    The proof that a real server treats these as ordinary text, finds no
    workspace, and leaves the table exactly as it was, is
    test_an_injection_shaped_slug_finds_nothing_and_leaves_the_table_alone in
    section D. This test is the cheap guard that runs on every commit; that
    one is the evidence.
    """
    connection = FakeConnection(row=None)

    found = await WorkspaceRepository().find_id_by_slug(connection, slug)

    query = normalize(connection.queries[0]["query"])

    assert found is None
    assert connection.queries[0]["args"] == (slug,)
    assert query == LOOKUP_SQL
    assert slug not in query


# --------------------------------------------------------------------------
# C. The service, over a fake pool and a fake repository
# --------------------------------------------------------------------------


class FakeWorkspaceRepository:
    """Records what it was handed and returns a canned id.

    Modelled on `FakeIssueRepository` in tests/conftest.py, and local to this
    file because this is the only place a workspace repository is faked. The
    connection is recorded alongside the slug: a repository that acquired its
    own connection instead of using the one it was given would be invisible to
    an assertion about the slug alone.
    """

    def __init__(self, workspace_id: UUID | None = None):
        self.workspace_id = workspace_id
        self.calls: list[dict] = []

    async def find_id_by_slug(self, connection, slug):
        self.calls.append({"connection": connection, "slug": slug})

        return self.workspace_id


def build_service(workspace_id: UUID | None = None):
    """The real service over a fake pool and a fake repository."""
    pool = FakePool()
    repository = FakeWorkspaceRepository(workspace_id)

    return WorkspaceService(pool=pool, repository=repository), pool, repository


async def test_scope_for_slug_resolves_through_the_pool_and_the_repository():
    """One acquisition, the pool's own connection, the caller's own slug.

    The identity check on the connection is the load-bearing one. Repositories
    in this codebase are handed a connection and must never reach for the pool
    themselves; a repository that did would still return the right id here,
    and only the object it was called with distinguishes the two.
    """
    service, pool, repository = build_service(BOOTSTRAP_WORKSPACE_ID)

    scope = await service.scope_for_slug(BOOTSTRAP_SLUG)

    assert pool.acquire_count == 1
    assert len(repository.calls) == 1
    assert repository.calls[0]["connection"] is pool.connection
    assert repository.calls[0]["slug"] == BOOTSTRAP_SLUG

    assert isinstance(scope, WorkspaceScope)
    assert scope == WorkspaceScope(workspace_id=BOOTSTRAP_WORKSPACE_ID)


async def test_resolving_the_same_slug_twice_performs_two_lookups():
    """No memoisation, because a cache here is shared tenant state by another name.

    The shipped service has no cache, so this is not a defect being pinned --
    it is a shape the suite has to refuse before someone adds it "for
    performance". Three lines do it: a dict in `__init__`, an early return on
    hit, a store before returning. Nothing else in this file notices, because
    every other test resolves each slug once.

    What makes it worth its own test is that it defeats the design's purpose
    by a route the rest of section A never covers. `WorkspaceScope` is frozen
    and passed explicitly, so no request can *overwrite* another request's
    tenant -- but a cache reintroduces exactly that failure without mutating
    anything. `WorkspaceService` is a natural singleton behind dependency
    injection, so one instance's dict is shared by every concurrent request;
    the same argument the service's own docstring makes against a module-level
    "current workspace" applies unchanged to a per-instance dict of them.

    The stale entry is where it becomes cross-tenant exposure. Slugs are
    reassignable: a workspace is renamed, deleted, or its slug is taken over
    by a different tenant, and a cached entry then answers that slug with the
    *previous* tenant's id. Every caller downstream trusts the scope it was
    handed, so the read that follows is scoped to a workspace the client never
    named -- silently, and with the suite green.

    Counted at the service level rather than observed against a real server on
    purpose: absence of caching is awkward to demonstrate through PostgreSQL
    and trivial to assert here, where the call is either made twice or it is
    not. The pool count is asserted alongside the repository count because a
    cache short-circuits both, and a future one that memoised the connection
    rather than the result would show up in only one of them.
    """
    service, pool, repository = build_service(BOOTSTRAP_WORKSPACE_ID)

    first = await service.scope_for_slug(BOOTSTRAP_SLUG)
    second = await service.scope_for_slug(BOOTSTRAP_SLUG)

    assert len(repository.calls) == 2
    assert pool.acquire_count == 2

    # Stated so a failure says *which* lookup went missing rather than only
    # that one did, and so the test cannot pass by resolving a second,
    # different slug.
    assert [call["slug"] for call in repository.calls] == [
        BOOTSTRAP_SLUG,
        BOOTSTRAP_SLUG,
    ]

    assert first == second


async def test_an_unresolved_slug_raises_instead_of_falling_back_to_a_workspace():
    """The failure this whole layer exists to make impossible.

    The tempting shape is a fallback: the slug matched nothing, so use the
    bootstrap workspace. It would look harmless while the bootstrap tenant is
    the only one with data, and it is the single change that would make every
    unrecognised request read and write another tenant's rows.

    Three things are asserted, because each admits a different version of it.
    That the call raises rules out returning a scope. That the repository was
    called exactly once rules out a second, quieter lookup for a default
    workspace. That the exception's args are unchanged rules out the slug
    riding along into a message a client or a log could see -- the reasoning
    for which is in WorkspaceNotFoundError's own docstring.
    """
    service, _, repository = build_service(workspace_id=None)

    with pytest.raises(WorkspaceNotFoundError) as error:
        await service.scope_for_slug("no-such-workspace")

    assert len(repository.calls) == 1
    assert repository.calls[0]["slug"] == "no-such-workspace"

    assert error.value.args == ("Workspace not found",)
    assert "no-such-workspace" not in str(error.value)
    assert not hasattr(error.value, "slug")


@pytest.mark.parametrize(
    "slug",
    ["vector", "Vector", "VECTOR", "vEcToR", " vector", "vector ", "acme"],
)
async def test_the_slug_reaches_the_repository_exactly_as_the_caller_wrote_it(slug):
    """Asserted on what the repository received, not on what came back.

    Whether a given slug resolves is the database's business and is settled in
    section D. The claim here is narrower and belongs to the service: it
    passes the string through. A `slug.lower()` or a `slug.strip()` on the way
    down is the case-insensitive tenant addressing that
    `workspaces_slug_format` was written to rule out, reintroduced one layer
    above the constraint where the constraint cannot see it.

    The fake resolves every slug, deliberately, so that a service which folded
    the input would still succeed and would still be caught.
    """
    service, _, repository = build_service(BOOTSTRAP_WORKSPACE_ID)

    await service.scope_for_slug(slug)

    assert repository.calls[0]["slug"] == slug


async def test_an_empty_slug_is_looked_up_rather_than_treated_as_absent():
    """The other shape a default workspace could take.

    "No slug given, so use the obvious one" is the same defect as the fallback
    above, arriving from the opposite direction: a caller that omitted the
    workspace entirely. The empty string has to travel to the lookup and miss
    there, like any other string that names no workspace.
    """
    service, pool, repository = build_service(workspace_id=None)

    with pytest.raises(WorkspaceNotFoundError):
        await service.scope_for_slug("")

    assert pool.acquire_count == 1
    assert repository.calls[0]["slug"] == ""


# --------------------------------------------------------------------------
# D. A real PostgreSQL 18, reached through the real migrations
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Resolved:
    service: WorkspaceService
    connection: asyncpg.Connection


@pytest.fixture
async def resolved(postgres_dsn):
    """001 then 002 through the runner, two workspaces, a real pool.

    The schema is built the way tests/test_migration_002_db.py's `applied`
    fixture builds it, and for the same reason: hand-written DDL would be a
    second definition of the schema, and the interesting claim is about the
    one the migrations produce. 001 goes in as its own text and 002 through
    `scripts.apply_migration`, which is the production genealogy -- a database
    that reached 001 before the ledger existed, and a runner that adopts it.

    Every table is dropped first. The container is shared for the whole
    session, so a `workspaces` left behind by an earlier file would make 002's
    `CREATE TABLE` fail here with a failure that has nothing to do with this
    test.

    The second workspace is inserted for *every* test in this section, not
    only the one that names it. With one workspace in the table, "the
    bootstrap slug resolves to the bootstrap id" is also satisfied by an
    implementation that ignores the slug and returns the only row it finds.

    The pool is small only because the rest of the db suite's pools are; its
    size is not load-bearing, and the teardown below is what makes a leaked
    connection visible.

    An earlier version of this docstring claimed that a `scope_for_slug` which
    failed to release its connection "would exhaust two connections partway
    through the repeated-lookup test rather than passing it". That was right
    that such a version does not pass and wrong that it *reports*: exhausting
    a pool is an await on a connection that is never coming, so the lane hung
    instead of failing. Mutating the service to leak showed it hangs earlier
    still -- `Pool.close()` waits for every connection to be released, so the
    first test in this section to leak one passes its body and then hangs here
    in teardown, and the repeated-lookup test is never even reached.

    So release is checked rather than provoked. The checked-out count is read
    while it still means something and asserted after the pool is gone, which
    turns a leak into a named failure on every test in this section instead of
    a CI job that times out with nothing attached to it.
    """
    connection = await asyncpg.connect(postgres_dsn)

    try:
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
            INSERT_WORKSPACE_SQL,
            SECOND_WORKSPACE_ID,
            SECOND_SLUG,
            "Acme",
        )

        pool = await asyncpg.create_pool(
            dsn=postgres_dsn,
            min_size=1,
            max_size=POOL_MAX_SIZE,
        )

        try:
            yield Resolved(
                service=WorkspaceService(pool=pool, repository=WorkspaceRepository()),
                connection=connection,
            )
        finally:
            # Read before the pool is torn down, because tearing it down is
            # what makes the answer uninteresting.
            checked_out = pool.get_size() - pool.get_idle_size()

            # `terminate()` rather than `await close()`. A graceful close waits
            # for every connection to be released, and a leaked one is never
            # released, so `close()` is precisely the hang this guard replaces.
            # Nothing is lost by the abrupt form: the server is a throwaway
            # container that the session removes.
            pool.terminate()

            assert checked_out == 0, (
                f"{checked_out} pooled connection(s) were still checked out "
                "when the test finished, so scope_for_slug acquired a "
                "connection and did not release it"
            )
    finally:
        await connection.close()


async def _snapshot(connection, sql: str) -> list[tuple]:
    return [tuple(row) for row in await connection.fetch(sql)]


@pytest.mark.db
async def test_the_bootstrap_slug_resolves_to_the_workspace_002_seeded(resolved):
    """The id is a literal from the migration, not a value read back first.

    Asserted against a constant because 002 writes the id as a literal for
    exactly this purpose. Fetching the expected value from `workspaces` would
    make this test agree with whatever the database happens to hold, including
    a database where the bootstrap row was never seeded.

    With `acme` also present, this rules out half the "just take a row"
    family: any `LIMIT 1` ordered by descending id, descending created_at or
    ascending slug returns the acme workspace here. The other half is ruled
    out by the test below.
    """
    scope = await resolved.service.scope_for_slug(BOOTSTRAP_SLUG)

    assert isinstance(scope, WorkspaceScope)
    assert scope.workspace_id == BOOTSTRAP_WORKSPACE_ID


@pytest.mark.db
async def test_a_second_workspace_resolves_to_its_own_id_and_not_the_bootstrap_one(
    resolved,
):
    """The assertion that makes resolution a lookup rather than a constant.

    An implementation that ignored its argument, or that ordered by ascending
    id or created_at and took one row, would return the bootstrap workspace
    for `acme` -- and would pass every other test in this section, because
    every other slug in the file either resolves to the bootstrap workspace or
    resolves to nothing.

    Both slugs are resolved in one test on purpose: a pair of assertions
    saying "these two slugs give two different ids, and each gives the right
    one" cannot be satisfied by any fixed answer at all.
    """
    bootstrap = await resolved.service.scope_for_slug(BOOTSTRAP_SLUG)
    second = await resolved.service.scope_for_slug(SECOND_SLUG)

    assert second.workspace_id == SECOND_WORKSPACE_ID
    assert bootstrap.workspace_id == BOOTSTRAP_WORKSPACE_ID
    assert second.workspace_id != bootstrap.workspace_id


@pytest.mark.db
@pytest.mark.parametrize("slug", ABSENT_SLUGS)
async def test_a_slug_no_workspace_holds_raises_workspace_not_found(resolved, slug):
    """Misses against a populated table, including the near ones.

    `acm`, `acmee`, `vecto` and the empty string are there so that the miss
    cannot be explained by the table being empty -- two workspaces are sitting
    right next to each of them. The LIKE metacharacters are the sharper half:
    under a pattern match rather than an equality, `%` matches both rows and a
    client sending it is handed a tenant it never named. On a fake connection
    that difference is invisible, because the fake answers with whatever row
    it was constructed with regardless of the SQL. Here the server decides.
    """
    with pytest.raises(WorkspaceNotFoundError):
        await resolved.service.scope_for_slug(slug)


@pytest.mark.db
@pytest.mark.parametrize("slug", CAPITALISED_SLUGS)
async def test_a_capitalised_slug_does_not_resolve_to_its_lowercase_twin(
    resolved,
    slug,
):
    """Case-sensitive addressing, proved against the server that stores it.

    The reason a capitalised slug misses is structural rather than incidental:
    `workspaces_slug_format` confines every stored slug to lowercase, so there
    is no row `Vector` could match and none can be created. That the
    constraint is present and rejects such an insert is pinned in
    test_migration_002_db.py and is not repeated here; what this pins is that
    resolution does not undo it with a `LOWER()` one layer up, where the
    constraint would go on passing while `Vector` and `vector` quietly became
    the same tenant.

    The lowercase form is resolved in the same test so that the miss cannot be
    read as "this fixture has no such workspace". It has one; only its
    capitalisation differs.
    """
    with pytest.raises(WorkspaceNotFoundError):
        await resolved.service.scope_for_slug(slug)

    scope = await resolved.service.scope_for_slug(slug.lower())

    assert scope.workspace_id in {BOOTSTRAP_WORKSPACE_ID, SECOND_WORKSPACE_ID}


@pytest.mark.db
@pytest.mark.parametrize("slug", INJECTION_SLUGS)
async def test_an_injection_shaped_slug_finds_nothing_and_leaves_the_table_alone(
    resolved,
    slug,
):
    """The evidence the section B tripwire is not.

    Section B proves the slug never enters the statement text. It cannot prove
    what a server would do with one of these strings, because no server ran
    them. This does: PostgreSQL parses the statement once, with `$1` as a
    placeholder, and the string arrives afterwards as data. So the trailing
    `--` comments nothing out, the `OR TRUE` widens nothing, and the two
    statements after a `;` are never statements at all -- they are part of a
    slug that no workspace has.

    `to_regclass` is checked because the interesting failure is not an error;
    it is a `DROP TABLE` that succeeded and left the next assertion looking at
    a table that no longer exists. Both rows are read back for the same
    reason: an `UPDATE ... SET slug = 'pwned'` would leave the table present
    and the count intact.
    """
    with pytest.raises(WorkspaceNotFoundError):
        await resolved.service.scope_for_slug(slug)

    assert await resolved.connection.fetchval(
        "SELECT to_regclass('public.workspaces') IS NOT NULL"
    )

    rows = await resolved.connection.fetch(
        "SELECT id, slug FROM workspaces ORDER BY slug"
    )

    assert [tuple(row) for row in rows] == [
        (SECOND_WORKSPACE_ID, SECOND_SLUG),
        (BOOTSTRAP_WORKSPACE_ID, BOOTSTRAP_SLUG),
    ]


@pytest.mark.db
async def test_resolving_slugs_writes_nothing_at_all(resolved):
    """Every lookup in this file, run in sequence, against a byte-for-byte snapshot.

    Row counts are the weak version of this and would miss the mutations worth
    worrying about: an `UPDATE` that rewrote a slug, a last-seen timestamp
    stamped on the tenant that was just resolved, or a write-back that stored
    the same values again. So the snapshot carries `xmin` and `ctid` as well
    as the columns. `xmin` is the transaction that last wrote each tuple and
    `ctid` is where it physically sits; any `UPDATE` changes both, because
    PostgreSQL writes a new tuple version rather than editing one in place.
    An identical snapshot afterwards is therefore not "the values look the
    same" but "these are the same tuples, untouched".

    `teams` is snapshotted alongside `workspaces` even though nothing here
    reads it, so that a lookup which grew a join or a cascade shows up.

    The sequence is deliberately the whole vocabulary -- hits, near misses,
    metacharacters, capitalisations and injection shapes -- because a write
    would most plausibly hang off one of the unusual paths rather than off the
    ordinary one.
    """
    workspaces_before = await _snapshot(resolved.connection, WORKSPACE_SNAPSHOT_SQL)
    teams_before = await _snapshot(resolved.connection, TEAM_SNAPSHOT_SQL)

    assert len(workspaces_before) == 2, (
        "the fixture did not seed both workspaces, so the comparison below "
        "would be a comparison of nothing against nothing"
    )
    assert len(teams_before) == 1

    # Bounded because this is the one test here that acquires more connections
    # than the pool holds. The fixture's teardown check reports a leak on every
    # test in this section, but it only runs once a test *finishes*, and a
    # leak stalls this body at the third lookup -- so without a ceiling this
    # single test is still an unexplained CI timeout.
    try:
        async with asyncio.timeout(LOOKUP_SEQUENCE_TIMEOUT):
            for slug in (BOOTSTRAP_SLUG, SECOND_SLUG):
                await resolved.service.scope_for_slug(slug)

            for slug in ABSENT_SLUGS + CAPITALISED_SLUGS + INJECTION_SLUGS:
                with pytest.raises(WorkspaceNotFoundError):
                    await resolved.service.scope_for_slug(slug)
    except TimeoutError:
        raise AssertionError(
            f"the lookup sequence did not finish within "
            f"{LOOKUP_SEQUENCE_TIMEOUT:.0f}s. These are indexed single-row "
            f"reads of a two-row table, so the cause is almost certainly a "
            f"connection scope_for_slug acquired and did not release: the "
            f"pool holds {POOL_MAX_SIZE}, and once they are gone every "
            f"further lookup waits for one that is never coming"
        ) from None

    assert await _snapshot(resolved.connection, WORKSPACE_SNAPSHOT_SQL) == (
        workspaces_before
    )
    assert await _snapshot(resolved.connection, TEAM_SNAPSHOT_SQL) == teams_before
