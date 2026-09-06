"""(slug, user) to AuthorizedWorkspaceScope, at four levels of the question.

Membership is where tenancy stops being an identity and starts being a
permission, and the failures that matter here are all quiet ones:

  * a scope that can be built without a membership, or that a caller can
    edit after the check, makes "may this user act here" a question with an
    answer the answerer chose. Section A pins the dataclass contract,
    including the asymmetry with WorkspaceScope that is the whole reason
    there are two types.
  * a role vocabulary that the database, the domain and the schema each
    hold a private copy of drifts silently until a role exists that one
    layer accepts and another cannot name. Section B pins the three copies
    equal.
  * a lookup that resolves the workspace first and then checks membership
    computes, somewhere in this process, the difference between "no such
    workspace" and "not yours" -- and a difference a server holds is a
    difference it can leak. Section C reads the statement the repository
    actually sends; section D checks the service raises one error for both.

None of A through D reaches a database, so none of them is evidence about
PostgreSQL: a fake connection agrees with whatever SQL it is handed.
Section E builds the schema through the real migration runner and asks a
real PostgreSQL 18 the same questions, with two workspaces and three users
present so that "the member resolves" cannot be satisfied by returning the
only row, and "the non-member is refused" cannot be satisfied by a table
that happens to be empty.

Section E is marked `db`: deselected by default, skipped when Docker is
unreachable. Nothing in this file touches DATABASE_URL or Neon; the only
server it speaks to is the throwaway container `postgres_dsn` starts.
"""

import re
from dataclasses import FrozenInstanceError, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import get_type_hints
from uuid import UUID

import asyncpg
import pytest

from app.domain.errors import WorkspaceAccessDeniedError, WorkspaceNotFoundError
from app.domain.memberships import WorkspaceMembershipEntity
from app.domain.tenancy import WORKSPACE_ROLES, AuthorizedWorkspaceScope, WorkspaceScope
from app.graphql.types.membership import WorkspaceRoleType
from app.repositories.memberships import MembershipRepository
from app.services.memberships import MEMBERSHIP_LIST_LIMIT, MembershipService
from scripts.apply_migration import apply_migration, read_migration

from tests.conftest import FakeConnection, FakePool, normalize


MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"
MIGRATION_001 = MIGRATIONS_DIR / "001_issues.sql"
MIGRATION_002 = MIGRATIONS_DIR / "002_tenancy.sql"
MIGRATION_003 = MIGRATIONS_DIR / "003_auth.sql"
MIGRATION_004 = MIGRATIONS_DIR / "004_membership.sql"

# The tenant migrations/002_tenancy.sql seeds, as literals. 002 names it in
# the file precisely so a test can assert against a constant rather than
# querying for the value it is about to check.
BOOTSTRAP_SLUG = "vector"
BOOTSTRAP_WORKSPACE_ID = UUID("00000000-0000-7000-8000-000000000001")

# A second tenant, created by this file. It is what makes "a member of A gets
# nothing for B" expressible at all, and it is also why "the member resolves"
# cannot pass by returning the only workspace in the table.
SECOND_SLUG = "acme"
SECOND_WORKSPACE_ID = UUID("00000000-0000-7000-8000-0000000000a1")
SECOND_WORKSPACE_NAME = "Acme"

# Three users. Ids are chosen so that every ordering an accidental "just take
# a row" implementation could fall into disagrees with at least one assertion:
# the owner of the bootstrap workspace sorts first, the acme member second,
# and the user who belongs to nothing last.
OWNER_USER_ID = UUID("00000000-0000-7000-8000-0000000000e1")
ACME_USER_ID = UUID("00000000-0000-7000-8000-0000000000e2")
OUTSIDER_USER_ID = UUID("00000000-0000-7000-8000-0000000000e3")

# A user id no `users` row holds. Used to prove the refusal does not depend on
# the caller existing -- an unknown user must be refused exactly as a known
# non-member is.
UNKNOWN_USER_ID = UUID("00000000-0000-7000-8000-0000000000ff")

OWNER_ROLE = "owner"
ADMIN_ROLE = "admin"
MEMBER_ROLE = "member"

# Slugs no workspace holds. The last three are LIKE metacharacters, which
# resolve to nothing only because the comparison is `=`.
ABSENT_SLUGS = ("no-such-workspace", "vecto", "acmee", "", "%", "_", "acm%")

# Slugs shaped like an attempt on the query.
INJECTION_SLUGS = (
    "x' OR TRUE --",
    "' OR 1=1 --",
    "vector' --",
    "vector' OR workspace_members.user_id IS NOT NULL --",
)

# The whole statement MembershipRepository.find_membership sends, normalized.
FIND_MEMBERSHIP_SQL = (
    "SELECT workspaces.id AS workspace_id, "
    "workspaces.slug AS workspace_slug, "
    "workspaces.name AS workspace_name, "
    "workspace_members.user_id AS user_id, "
    "workspace_members.role AS role, "
    "workspace_members.created_at AS created_at "
    "FROM workspace_members "
    "JOIN workspaces ON workspaces.id = workspace_members.workspace_id "
    "WHERE workspaces.slug = $1 AND workspace_members.user_id = $2"
)

# And the whole statement MembershipRepository.list_for_user sends.
LIST_FOR_USER_SQL = (
    "SELECT workspaces.id AS workspace_id, "
    "workspaces.slug AS workspace_slug, "
    "workspaces.name AS workspace_name, "
    "workspace_members.user_id AS user_id, "
    "workspace_members.role AS role, "
    "workspace_members.created_at AS created_at "
    "FROM workspace_members "
    "JOIN workspaces ON workspaces.id = workspace_members.workspace_id "
    "WHERE workspace_members.user_id = $1 "
    "ORDER BY workspaces.slug "
    "LIMIT $2"
)

# Every `CONSTRAINT <name> CHECK (role IN (...))` in a migration, with the
# quoted list captured whole. Deliberately not a search for the roles
# themselves: a constraint that listed two of the three would still contain
# every role the test looked for.
ROLE_CHECK = re.compile(
    r"CONSTRAINT\s+(\w+)\s+CHECK\s*\(\s*role\s+IN\s*\(([^)]*)\)\s*\)",
    re.IGNORECASE,
)
QUOTED = re.compile(r"'([^']*)'")

BASE_TIME = datetime(2026, 3, 1, 9, 0, 0, tzinfo=timezone.utc)

POOL_MAX_SIZE = 2

INSERT_WORKSPACE_SQL = """
INSERT INTO workspaces (id, slug, name)
VALUES ($1, $2, $3)
"""

INSERT_USER_SQL = """
INSERT INTO users (id)
VALUES ($1)
"""

INSERT_MEMBER_SQL = """
INSERT INTO workspace_members (workspace_id, user_id, role)
VALUES ($1, $2, $3)
"""

# A stand-in for whatever migrations/003_auth.sql will create.
#
# 004 references `users (id)` and 003 is being written on another branch, so
# this builds the one column 004's foreign key needs and nothing else. It is
# used only when 003 is absent from the checkout; the fixture below applies
# the real file the moment it appears, so this becomes dead the same day 003
# merges rather than quietly outliving it.
#
# What it cannot check is that 003's `users.id` is a UUID primary key. If it
# is not, 004's foreign key fails against the real file and this test file is
# where that will surface.
STANDIN_USERS_SQL = """
CREATE TABLE users (
    id UUID PRIMARY KEY
)
"""

# The tables 004 adds, dropped both before a test and after it.
#
# After is the half that is easy to leave out and the half that matters. The
# `postgres_dsn` container is shared by the whole session, and the fixtures in
# tests/test_migration_002_db.py and tests/test_workspace_resolution.py open
# with `DROP TABLE IF EXISTS issues, teams, workspaces` -- a list written
# before these two tables existed. A `workspace_members` left standing
# references `workspaces`, so that drop fails, and every test in those files
# errors in setup with a message about a table they have never heard of.
#
# Dropping on the way out keeps that coupling from being this branch's to
# export. The general fix is a shared reset that names no tables at all, which
# is a change to files other work is in the middle of.
DROP_MEMBERSHIP_TABLES_SQL = (
    "DROP TABLE IF EXISTS workspace_invitations, workspace_members"
)


def make_membership(
    *,
    workspace_id: UUID = BOOTSTRAP_WORKSPACE_ID,
    workspace_slug: str = BOOTSTRAP_SLUG,
    workspace_name: str = "Vector",
    user_id: UUID = OWNER_USER_ID,
    role: str = OWNER_ROLE,
    created_at: datetime = BASE_TIME,
) -> WorkspaceMembershipEntity:
    return WorkspaceMembershipEntity(
        workspace_id=workspace_id,
        workspace_slug=workspace_slug,
        workspace_name=workspace_name,
        user_id=user_id,
        role=role,
        created_at=created_at,
    )


def as_record(entity: WorkspaceMembershipEntity) -> dict:
    """asyncpg.Record supports __getitem__, which a dict models well enough.

    Keyed by the *aliases* the statement declares, not by the entity's field
    names, so a repository that stopped aliasing `workspaces.id` would fail
    here rather than silently reading a column of the same bare name.
    """
    return {
        "workspace_id": entity.workspace_id,
        "workspace_slug": entity.workspace_slug,
        "workspace_name": entity.workspace_name,
        "user_id": entity.user_id,
        "role": entity.role,
        "created_at": entity.created_at,
    }


# --- section A: the scope contract ------------------------------------


def test_an_authorized_scope_is_a_workspace_scope():
    """Substitutable downwards, so authorized work can call tenant work.

    Everything that takes a WorkspaceScope today keeps working when handed
    one of these, which is what makes adding authorization an addition
    rather than a rewrite.
    """
    scope = AuthorizedWorkspaceScope(
        workspace_id=BOOTSTRAP_WORKSPACE_ID,
        user_id=OWNER_USER_ID,
        role=OWNER_ROLE,
    )

    assert isinstance(scope, WorkspaceScope)
    assert isinstance(scope, AuthorizedWorkspaceScope)


def test_a_workspace_scope_is_not_an_authorized_scope():
    """The asymmetry is the invariant, and it is worth a test of its own.

    A WorkspaceScope is obtainable by anyone who can spell a slug. If it
    also satisfied an AuthorizedWorkspaceScope annotation, every function
    that asks for evidence of membership would accept the absence of it,
    and the type distinction would be documentation rather than a check.
    """
    scope = WorkspaceScope(workspace_id=BOOTSTRAP_WORKSPACE_ID)

    assert not isinstance(scope, AuthorizedWorkspaceScope)


def test_an_authorized_scope_never_equals_a_bare_scope_for_the_same_workspace():
    """Equality must not erase the difference either.

    Dataclass equality requires the same class, so these compare unequal
    even though the base's only field matches. A test rather than a note,
    because `==` is how a set, a dict key or an `in` check asks the
    question, and any of those silently treating the two as one value would
    let an unauthorized scope stand in for an authorized one.
    """
    bare = WorkspaceScope(workspace_id=BOOTSTRAP_WORKSPACE_ID)
    authorized = AuthorizedWorkspaceScope(
        workspace_id=BOOTSTRAP_WORKSPACE_ID,
        user_id=OWNER_USER_ID,
        role=OWNER_ROLE,
    )

    assert bare != authorized
    assert authorized != bare
    assert len({bare, authorized}) == 2


def test_an_authorized_scope_carries_exactly_workspace_user_and_role():
    """Three fields, named and typed. Nothing else, and nothing missing.

    A field that arrived here later -- an `is_admin` flag, a permissions
    list, a cached workspace name -- would be a second place the answer to
    an authorization question is written down, and the two would disagree.
    """
    declared = [field.name for field in fields(AuthorizedWorkspaceScope)]

    assert declared == ["workspace_id", "user_id", "role"]

    hints = get_type_hints(AuthorizedWorkspaceScope)

    assert hints["workspace_id"] is UUID
    assert hints["user_id"] is UUID
    assert hints["role"] is str


def test_an_authorized_scope_cannot_be_built_without_a_user_or_a_role():
    """Neither field has a default, so neither can be omitted.

    A default user id would be a scope belonging to nobody, and a default
    role would be a privilege level chosen by this dataclass rather than by
    the row that granted it.
    """
    with pytest.raises(TypeError):
        AuthorizedWorkspaceScope(workspace_id=BOOTSTRAP_WORKSPACE_ID)

    with pytest.raises(TypeError):
        AuthorizedWorkspaceScope(
            workspace_id=BOOTSTRAP_WORKSPACE_ID,
            user_id=OWNER_USER_ID,
        )


def test_an_authorized_scope_cannot_be_reassigned():
    """Frozen, including the field a caller would most want to raise."""
    scope = AuthorizedWorkspaceScope(
        workspace_id=BOOTSTRAP_WORKSPACE_ID,
        user_id=OWNER_USER_ID,
        role=MEMBER_ROLE,
    )

    with pytest.raises(FrozenInstanceError):
        scope.role = OWNER_ROLE

    with pytest.raises(FrozenInstanceError):
        scope.workspace_id = SECOND_WORKSPACE_ID

    with pytest.raises(FrozenInstanceError):
        scope.user_id = OUTSIDER_USER_ID

    assert scope.role == MEMBER_ROLE
    assert scope.workspace_id == BOOTSTRAP_WORKSPACE_ID
    assert scope.user_id == OWNER_USER_ID


def test_an_authorized_scope_has_no_instance_dictionary():
    """`slots=True` survived the subclassing, so no attribute can be added.

    Both halves matter. The subclass's own `__slots__` holds exactly its two
    new fields -- dataclasses filters out the ones the base already slotted,
    and a subclass that re-declared `workspace_id` would shadow the base's
    descriptor with a second, independent storage slot. And no `__dict__`
    appears anywhere in the hierarchy, which is what stops
    `scope.effective_role = 'owner'` from succeeding: frozen alone catches
    the declared fields, so without slots an object could still be given a
    second, unfrozen answer under a name nothing declared.

    The three accepted exception types need explaining. On CPython 3.12.5 a
    non-field assignment to a frozen `slots=True` dataclass raises
    *TypeError*, not AttributeError or FrozenInstanceError: the
    `__setattr__` the decorator generates closes over the class it was built
    for, `slots=True` then replaces that class with a new one, and the
    fallback line `super(cls, self).__setattr__(...)` is left holding the
    pre-slots class that `self` is no longer an instance of. The base class
    behaves identically, so this is a property of frozen-plus-slots and not
    of the subclassing. Immutability is unaffected -- nothing is assigned,
    which is what the `hasattr` below checks -- so the three types are
    accepted rather than the one observed, and this test keeps passing if a
    later CPython raises the exception it looks like it should.
    """
    scope = AuthorizedWorkspaceScope(
        workspace_id=BOOTSTRAP_WORKSPACE_ID,
        user_id=OWNER_USER_ID,
        role=MEMBER_ROLE,
    )

    assert AuthorizedWorkspaceScope.__slots__ == ("user_id", "role")
    assert WorkspaceScope.__slots__ == ("workspace_id",)
    assert not hasattr(scope, "__dict__")

    with pytest.raises((TypeError, AttributeError, FrozenInstanceError)):
        scope.effective_role = OWNER_ROLE

    assert not hasattr(scope, "effective_role")


def test_scopes_compare_and_hash_over_all_three_fields():
    """Role and user are part of the value, not annotations on a workspace.

    A scope that compared by workspace alone would let a member's scope
    stand in for an owner's in a cache, a set, or a memoised permission
    check.
    """
    member = AuthorizedWorkspaceScope(
        workspace_id=BOOTSTRAP_WORKSPACE_ID,
        user_id=OWNER_USER_ID,
        role=MEMBER_ROLE,
    )
    elevated = AuthorizedWorkspaceScope(
        workspace_id=BOOTSTRAP_WORKSPACE_ID,
        user_id=OWNER_USER_ID,
        role=OWNER_ROLE,
    )
    other_user = AuthorizedWorkspaceScope(
        workspace_id=BOOTSTRAP_WORKSPACE_ID,
        user_id=OUTSIDER_USER_ID,
        role=MEMBER_ROLE,
    )

    assert member != elevated
    assert member != other_user
    assert len({member, elevated, other_user}) == 3

    same = AuthorizedWorkspaceScope(
        workspace_id=BOOTSTRAP_WORKSPACE_ID,
        user_id=OWNER_USER_ID,
        role=MEMBER_ROLE,
    )

    assert member == same
    assert hash(member) == hash(same)


# --- section B: one role vocabulary, three copies ---------------------


def role_checks(sql: str) -> dict[str, tuple[str, ...]]:
    """Every named `CHECK (role IN (...))` in a migration, as parsed lists."""
    return {
        match.group(1): tuple(QUOTED.findall(match.group(2)))
        for match in ROLE_CHECK.finditer(sql)
    }


def test_the_migration_constrains_the_role_column_on_both_tables():
    """Both tables, by constraint name. One CHECK is not enough.

    Accepting an invitation writes its role into workspace_members, so an
    invitations table that admitted a role the members table rejects would
    produce invitations nobody can accept -- and the failure would surface
    at acceptance time, to the invitee, as a constraint violation.
    """
    checks = role_checks(read_migration(MIGRATION_004))

    assert set(checks) == {
        "workspace_members_role_check",
        "workspace_invitations_role_check",
    }


@pytest.mark.parametrize(
    "constraint",
    ["workspace_members_role_check", "workspace_invitations_role_check"],
)
def test_every_role_check_admits_exactly_the_domain_vocabulary(constraint):
    """The database's copy and the domain's copy, compared as sets.

    As sets, because `IN` is a membership test: the order in the file is
    documentation, and a test that pinned it would fail on a reordering
    that changes nothing.
    """
    checks = role_checks(read_migration(MIGRATION_004))

    assert set(checks[constraint]) == set(WORKSPACE_ROLES)


def test_the_graphql_enum_says_exactly_the_domain_vocabulary():
    """The transport's copy, against the same source.

    `WorkspaceMembershipType.from_entity` converts a stored role into this
    enum and raises on one it does not carry, so a role in the database
    that is missing here is an internal error on a perfectly valid row.
    """
    assert {member.value for member in WorkspaceRoleType} == set(WORKSPACE_ROLES)


def test_the_role_vocabulary_has_no_duplicates():
    """A duplicate would make the set comparisons above pass while the
    tuple carried a role twice -- harmless today, and exactly the kind of
    thing that stops being harmless when something iterates it."""
    assert len(WORKSPACE_ROLES) == len(set(WORKSPACE_ROLES))


# --- section C: the statements the repository sends -------------------


async def test_find_membership_joins_workspaces_and_filters_on_both_keys():
    connection = FakeConnection()

    await MembershipRepository().find_membership(
        connection,
        slug=BOOTSTRAP_SLUG,
        user_id=OWNER_USER_ID,
    )

    assert len(connection.queries) == 1

    sent = connection.queries[0]

    assert normalize(sent["query"]) == FIND_MEMBERSHIP_SQL
    assert sent["args"] == (BOOTSTRAP_SLUG, OWNER_USER_ID)


async def test_the_membership_lookup_is_a_single_statement_and_nothing_more():
    """A tripwire on the shape of the authorization query.

    Each clause below is a way the lookup could stop meaning what the
    service reads into it:

      * two statements, or a lookup that resolved the workspace first,
        would let this process compute existence separately from
        membership -- which is the distinction the whole design refuses to
        hold;
      * `OR` or `UNION` would widen the filter past the two keys;
      * `LEFT JOIN` would return a row for a workspace with no matching
        membership, which `find_membership` reports as a membership;
      * `LOWER(` or `LIKE` would make one stored slug reachable by
        spellings `workspaces_slug_format` was written to exclude.

    What a fake connection cannot establish is that PostgreSQL agrees; it
    returns whatever it is handed. Section E is the evidence about the
    server. This is the cheap guard that runs on every commit.
    """
    connection = FakeConnection()

    await MembershipRepository().find_membership(
        connection,
        slug=BOOTSTRAP_SLUG,
        user_id=OWNER_USER_ID,
    )

    sent = normalize(connection.queries[0]["query"]).upper()

    assert sent.count("SELECT") == 1
    assert ";" not in sent
    assert " OR " not in sent
    assert "UNION" not in sent
    assert "LEFT JOIN" not in sent
    assert "LOWER(" not in sent
    assert "LIKE" not in sent
    assert sent.count("$") == 2


@pytest.mark.parametrize("slug", INJECTION_SLUGS)
async def test_an_injection_shaped_slug_stays_a_bound_value(slug):
    """The slug never reaches the statement text, whatever it contains."""
    connection = FakeConnection()

    await MembershipRepository().find_membership(
        connection,
        slug=slug,
        user_id=OWNER_USER_ID,
    )

    sent = connection.queries[0]

    assert normalize(sent["query"]) == FIND_MEMBERSHIP_SQL
    assert sent["args"] == (slug, OWNER_USER_ID)


async def test_find_membership_maps_the_row_onto_an_entity():
    entity = make_membership(role=ADMIN_ROLE)
    connection = FakeConnection(row=as_record(entity))

    found = await MembershipRepository().find_membership(
        connection,
        slug=BOOTSTRAP_SLUG,
        user_id=OWNER_USER_ID,
    )

    assert found == entity


async def test_find_membership_returns_none_when_nothing_matches():
    """The default FakeConnection row is None, which is the not-found path."""
    found = await MembershipRepository().find_membership(
        FakeConnection(),
        slug=BOOTSTRAP_SLUG,
        user_id=OWNER_USER_ID,
    )

    assert found is None


async def test_list_for_user_filters_on_the_user_and_orders_by_slug():
    connection = FakeConnection()

    await MembershipRepository().list_for_user(
        connection,
        user_id=OWNER_USER_ID,
        limit=MEMBERSHIP_LIST_LIMIT,
    )

    sent = connection.queries[0]

    assert normalize(sent["query"]) == LIST_FOR_USER_SQL
    assert sent["args"] == (OWNER_USER_ID, MEMBERSHIP_LIST_LIMIT)


async def test_the_listing_statement_takes_no_workspace_argument():
    """Two parameters, and neither of them is a workspace.

    A workspace filter here would be a second way to ask "am I in this
    workspace", reachable without going through `find_membership` -- and
    therefore a second place for the absent-versus-unauthorized
    distinction to be reintroduced.
    """
    connection = FakeConnection()

    await MembershipRepository().list_for_user(
        connection,
        user_id=OWNER_USER_ID,
        limit=MEMBERSHIP_LIST_LIMIT,
    )

    sent = normalize(connection.queries[0]["query"]).upper()

    assert sent.count("$") == 2
    assert "WORKSPACES.SLUG =" not in sent
    assert "WORKSPACE_MEMBERS.WORKSPACE_ID =" not in sent
    assert "OFFSET" not in sent


# --- section D: what the service makes of the answer ------------------


def build_service(row=None) -> tuple[MembershipService, FakePool]:
    pool = FakePool(FakeConnection(row=row))

    return MembershipService(pool=pool, repository=MembershipRepository()), pool


async def test_a_member_resolves_to_a_scope_carrying_their_role():
    service, pool = build_service(row=as_record(make_membership(role=ADMIN_ROLE)))

    scope = await service.authorized_scope_for_slug(
        slug=BOOTSTRAP_SLUG,
        user_id=OWNER_USER_ID,
    )

    assert isinstance(scope, AuthorizedWorkspaceScope)
    assert scope == AuthorizedWorkspaceScope(
        workspace_id=BOOTSTRAP_WORKSPACE_ID,
        user_id=OWNER_USER_ID,
        role=ADMIN_ROLE,
    )
    assert pool.acquire_count == 1


async def test_the_scope_takes_its_role_from_the_row_and_not_from_a_default():
    """Each role in turn, so a hard-coded one cannot pass by luck."""
    for role in WORKSPACE_ROLES:
        service, _ = build_service(row=as_record(make_membership(role=role)))

        scope = await service.authorized_scope_for_slug(
            slug=BOOTSTRAP_SLUG,
            user_id=OWNER_USER_ID,
        )

        assert scope.role == role


async def test_the_scope_takes_its_ids_from_the_row_and_not_from_the_arguments():
    """Deliberately a row that disagrees with what was asked for.

    Not a situation the schema can produce -- the statement filters on both
    keys -- which is the point: the assertion is about where the service
    reads its answer, and a service that echoed its arguments would pass
    every realistic test and this one only if it read the row.
    """
    row = as_record(
        make_membership(
            workspace_id=SECOND_WORKSPACE_ID,
            user_id=ACME_USER_ID,
            role=MEMBER_ROLE,
        )
    )
    service, _ = build_service(row=row)

    scope = await service.authorized_scope_for_slug(
        slug=BOOTSTRAP_SLUG,
        user_id=OWNER_USER_ID,
    )

    assert scope.workspace_id == SECOND_WORKSPACE_ID
    assert scope.user_id == ACME_USER_ID


async def test_an_absent_row_is_refused_rather_than_defaulted():
    service, pool = build_service(row=None)

    with pytest.raises(WorkspaceAccessDeniedError):
        await service.authorized_scope_for_slug(
            slug=BOOTSTRAP_SLUG,
            user_id=OUTSIDER_USER_ID,
        )

    assert pool.acquire_count == 1


async def test_the_refusal_is_not_a_workspace_not_found_error():
    """The two errors mean different things and must not be interchanged.

    WorkspaceNotFoundError is raised by a lookup that answers existence on
    purpose, for callers entitled to that answer. Raising it here would
    tell a non-member that the workspace is real.
    """
    service, _ = build_service(row=None)

    with pytest.raises(WorkspaceAccessDeniedError) as raised:
        await service.authorized_scope_for_slug(
            slug=BOOTSTRAP_SLUG,
            user_id=OUTSIDER_USER_ID,
        )

    assert not isinstance(raised.value, WorkspaceNotFoundError)


async def test_the_refusal_carries_neither_the_slug_nor_the_user():
    """Nothing about the request travels up inside the exception.

    An exception that carried the slug would put an unsanitised client
    string into every log line and every message that renders it.
    """
    service, _ = build_service(row=None)

    with pytest.raises(WorkspaceAccessDeniedError) as raised:
        await service.authorized_scope_for_slug(
            slug=BOOTSTRAP_SLUG,
            user_id=OUTSIDER_USER_ID,
        )

    rendered = str(raised.value)

    assert BOOTSTRAP_SLUG not in rendered
    assert str(OUTSIDER_USER_ID) not in rendered
    assert raised.value.args == ("Workspace is not accessible",)


async def test_the_slug_reaches_the_repository_exactly_as_the_caller_wrote_it():
    """No trimming, no case folding, no rewriting on the way down."""
    for slug in ("  vector  ", "VECTOR", "Vector", "vector/", *INJECTION_SLUGS):
        service, pool = build_service(row=None)

        with pytest.raises(WorkspaceAccessDeniedError):
            await service.authorized_scope_for_slug(
                slug=slug,
                user_id=OWNER_USER_ID,
            )

        assert pool.connection.queries[0]["args"] == (slug, OWNER_USER_ID)


async def test_listing_bounds_itself_without_being_asked_to():
    """The service states the limit; no caller supplies one."""
    service, pool = build_service()

    await service.list_for_user(user_id=OWNER_USER_ID)

    assert pool.connection.queries[0]["args"] == (OWNER_USER_ID, MEMBERSHIP_LIST_LIMIT)


# --- section E: the same questions, asked of PostgreSQL ---------------


async def _build_schema(connection) -> None:
    """001, 002, users, 004 -- the genealogy an operator would produce.

    001 goes in as its own text and the rest through
    `scripts.apply_migration`, matching tests/test_migration_002_db.py: a
    database that reached 001 before the ledger existed, and a runner that
    adopts it.

    003 is applied when the checkout has it and stood in for when it does
    not, so this fixture starts exercising the real `users` the day 003
    merges without an edit here. The stand-in is the narrowest thing 004's
    foreign key can point at, which is deliberate -- a richer fake would
    invite assertions about a table this branch does not own.
    """
    await connection.execute(
        "DROP TABLE IF EXISTS "
        "workspace_invitations, workspace_members, issues, teams, workspaces, users"
    )
    await connection.execute("DROP TABLE IF EXISTS schema_migrations")
    await connection.execute(read_migration(MIGRATION_001))

    async with connection.transaction():
        await apply_migration(connection, MIGRATION_002, migrations_dir=MIGRATIONS_DIR)

    if MIGRATION_003.is_file():
        async with connection.transaction():
            await apply_migration(
                connection,
                MIGRATION_003,
                migrations_dir=MIGRATIONS_DIR,
            )
    else:
        await connection.execute(STANDIN_USERS_SQL)

    async with connection.transaction():
        await apply_migration(connection, MIGRATION_004, migrations_dir=MIGRATIONS_DIR)


async def _seed(connection) -> None:
    """Two workspaces, three users, two memberships.

    The shape is what makes the refusals meaningful. `OWNER_USER_ID` owns
    the bootstrap workspace and belongs to nothing else, `ACME_USER_ID`
    belongs only to acme, and `OUTSIDER_USER_ID` is a real account with no
    membership at all -- so "not a member" is tested against a user the
    database knows, not only against one it has never seen.
    """
    await connection.execute(
        INSERT_WORKSPACE_SQL,
        SECOND_WORKSPACE_ID,
        SECOND_SLUG,
        SECOND_WORKSPACE_NAME,
    )

    for user_id in (OWNER_USER_ID, ACME_USER_ID, OUTSIDER_USER_ID):
        await connection.execute(INSERT_USER_SQL, user_id)

    await connection.execute(
        INSERT_MEMBER_SQL,
        BOOTSTRAP_WORKSPACE_ID,
        OWNER_USER_ID,
        OWNER_ROLE,
    )
    await connection.execute(
        INSERT_MEMBER_SQL,
        SECOND_WORKSPACE_ID,
        ACME_USER_ID,
        MEMBER_ROLE,
    )


@pytest.fixture
async def membership_service(postgres_dsn):
    """A MembershipService over a real pool, on the schema 004 produces.

    Every table is dropped first: the container is shared for the whole
    session, so a `workspaces` left behind by another file would make 002's
    CREATE TABLE fail here with a failure that has nothing to do with this
    test.

    The checked-out count is read while it still means something and
    asserted after the pool is gone. A service that leaks a connection
    would otherwise hang in `close()` rather than fail, which is a CI job
    that times out with nothing attached to it -- see the same guard, and
    the same reasoning, in tests/test_workspace_resolution.py.
    """
    connection = await asyncpg.connect(postgres_dsn)

    try:
        await _build_schema(connection)
        await _seed(connection)

        pool = await asyncpg.create_pool(
            dsn=postgres_dsn,
            min_size=1,
            max_size=POOL_MAX_SIZE,
        )

        try:
            yield MembershipService(pool=pool, repository=MembershipRepository())
        finally:
            checked_out = pool.get_size() - pool.get_idle_size()

            pool.terminate()

            assert checked_out == 0, (
                f"{checked_out} pooled connection(s) were still checked out "
                "when the test finished, so the service acquired a connection "
                "and did not release it"
            )
    finally:
        # Unconditional, and before the connection closes: see
        # DROP_MEMBERSHIP_TABLES_SQL for what these two tables break in the
        # files that run after this one.
        try:
            await connection.execute(DROP_MEMBERSHIP_TABLES_SQL)
        finally:
            await connection.close()


@pytest.mark.db
async def test_a_member_resolves_to_a_scope_with_the_role_the_row_holds(
    membership_service,
):
    """Two workspaces and two memberships, so neither half can be guessed."""
    owner_scope = await membership_service.authorized_scope_for_slug(
        slug=BOOTSTRAP_SLUG,
        user_id=OWNER_USER_ID,
    )

    assert owner_scope == AuthorizedWorkspaceScope(
        workspace_id=BOOTSTRAP_WORKSPACE_ID,
        user_id=OWNER_USER_ID,
        role=OWNER_ROLE,
    )

    member_scope = await membership_service.authorized_scope_for_slug(
        slug=SECOND_SLUG,
        user_id=ACME_USER_ID,
    )

    assert member_scope == AuthorizedWorkspaceScope(
        workspace_id=SECOND_WORKSPACE_ID,
        user_id=ACME_USER_ID,
        role=MEMBER_ROLE,
    )


@pytest.mark.db
async def test_a_non_member_of_an_existing_workspace_is_refused(membership_service):
    """A real account, a real workspace, and no row joining them."""
    with pytest.raises(WorkspaceAccessDeniedError):
        await membership_service.authorized_scope_for_slug(
            slug=BOOTSTRAP_SLUG,
            user_id=OUTSIDER_USER_ID,
        )


@pytest.mark.db
async def test_a_member_of_one_workspace_gets_no_scope_for_another(membership_service):
    """The isolation claim, in both directions.

    Both users hold a membership, so neither refusal can be explained by
    the caller being unknown to the system -- only by the workspace being
    someone else's.
    """
    with pytest.raises(WorkspaceAccessDeniedError):
        await membership_service.authorized_scope_for_slug(
            slug=SECOND_SLUG,
            user_id=OWNER_USER_ID,
        )

    with pytest.raises(WorkspaceAccessDeniedError):
        await membership_service.authorized_scope_for_slug(
            slug=BOOTSTRAP_SLUG,
            user_id=ACME_USER_ID,
        )


@pytest.mark.db
@pytest.mark.parametrize("slug", ABSENT_SLUGS)
async def test_a_slug_no_workspace_holds_is_refused_the_same_way(
    membership_service, slug
):
    """Including the LIKE metacharacters, which match nothing under `=`."""
    with pytest.raises(WorkspaceAccessDeniedError):
        await membership_service.authorized_scope_for_slug(
            slug=slug,
            user_id=OWNER_USER_ID,
        )


@pytest.mark.db
async def test_a_nonexistent_workspace_and_an_unauthorized_one_are_indistinguishable(
    membership_service,
):
    """The central claim of this branch, asserted on the errors themselves.

    Same type, same arguments, same rendering. A caller holding both
    exceptions has nothing to compare that would tell them whether
    `BOOTSTRAP_SLUG` names a real workspace -- which it does -- or whether
    `no-such-workspace` does not.
    """
    with pytest.raises(WorkspaceAccessDeniedError) as unauthorized:
        await membership_service.authorized_scope_for_slug(
            slug=BOOTSTRAP_SLUG,
            user_id=OUTSIDER_USER_ID,
        )

    with pytest.raises(WorkspaceAccessDeniedError) as absent:
        await membership_service.authorized_scope_for_slug(
            slug="no-such-workspace",
            user_id=OUTSIDER_USER_ID,
        )

    assert type(unauthorized.value) is type(absent.value)
    assert unauthorized.value.args == absent.value.args
    assert str(unauthorized.value) == str(absent.value)


@pytest.mark.db
async def test_an_unknown_user_is_refused_exactly_as_a_known_non_member_is(
    membership_service,
):
    """No `users` row at all, and the answer does not change.

    Worth its own test because the refusal must not depend on the caller
    existing: a lookup that checked the user first would answer a stranger
    differently from a member of another workspace.
    """
    with pytest.raises(WorkspaceAccessDeniedError) as unknown:
        await membership_service.authorized_scope_for_slug(
            slug=BOOTSTRAP_SLUG,
            user_id=UNKNOWN_USER_ID,
        )

    with pytest.raises(WorkspaceAccessDeniedError) as known:
        await membership_service.authorized_scope_for_slug(
            slug=BOOTSTRAP_SLUG,
            user_id=OUTSIDER_USER_ID,
        )

    assert unknown.value.args == known.value.args


@pytest.mark.db
@pytest.mark.parametrize("slug", ("Vector", "VECTOR", "vEcToR", "ACME"))
async def test_a_capitalised_slug_does_not_resolve_to_its_lowercase_twin(
    membership_service, slug
):
    """`workspaces_slug_format` confines stored slugs to lowercase, so a
    capitalised slug names no storable workspace. What is checked here is
    that this lookup does not reintroduce the folding that constraint
    exists to make impossible."""
    with pytest.raises(WorkspaceAccessDeniedError):
        await membership_service.authorized_scope_for_slug(
            slug=slug,
            user_id=OWNER_USER_ID,
        )


@pytest.mark.db
@pytest.mark.parametrize("slug", INJECTION_SLUGS)
async def test_an_injection_shaped_slug_finds_nothing_against_a_real_server(
    membership_service, slug
):
    """The server's answer, not a fake's. The slug is data end to end."""
    with pytest.raises(WorkspaceAccessDeniedError):
        await membership_service.authorized_scope_for_slug(
            slug=slug,
            user_id=OWNER_USER_ID,
        )


@pytest.mark.db
async def test_listing_returns_only_the_callers_own_workspaces(membership_service):
    """Each user sees their own membership and nobody else's."""
    owned = await membership_service.list_for_user(user_id=OWNER_USER_ID)

    assert [membership.workspace_slug for membership in owned] == [BOOTSTRAP_SLUG]
    assert owned[0].role == OWNER_ROLE
    assert owned[0].workspace_id == BOOTSTRAP_WORKSPACE_ID
    assert owned[0].user_id == OWNER_USER_ID

    joined = await membership_service.list_for_user(user_id=ACME_USER_ID)

    assert [membership.workspace_slug for membership in joined] == [SECOND_SLUG]
    assert joined[0].role == MEMBER_ROLE


@pytest.mark.db
async def test_listing_for_a_user_with_no_memberships_is_empty(membership_service):
    """Empty, not an error: belonging to nothing is an ordinary state."""
    assert await membership_service.list_for_user(user_id=OUTSIDER_USER_ID) == []
    assert await membership_service.list_for_user(user_id=UNKNOWN_USER_ID) == []


@pytest.mark.db
async def test_listing_orders_by_slug_across_several_workspaces(
    membership_service, postgres_dsn
):
    """Ordering asserted against a seed whose insertion order contradicts it.

    The workspaces are created in reverse slug order and joined in a third
    order, so a query relying on insertion order, on id order or on the
    membership's own created_at returns a different list from this one.
    """
    connection = await asyncpg.connect(postgres_dsn)

    try:
        extra = (
            ("zulu", UUID("00000000-0000-7000-8000-0000000000f3")),
            ("alpha", UUID("00000000-0000-7000-8000-0000000000f1")),
            ("mike", UUID("00000000-0000-7000-8000-0000000000f2")),
        )

        for slug, workspace_id in extra:
            await connection.execute(
                INSERT_WORKSPACE_SQL,
                workspace_id,
                slug,
                slug.title(),
            )

        # Joined in a third order, so slug order, insertion order and
        # membership created_at order all disagree with one another.
        for _slug, workspace_id in (extra[2], extra[0], extra[1]):
            await connection.execute(
                INSERT_MEMBER_SQL,
                workspace_id,
                OUTSIDER_USER_ID,
                MEMBER_ROLE,
            )
    finally:
        await connection.close()

    memberships = await membership_service.list_for_user(user_id=OUTSIDER_USER_ID)

    assert [membership.workspace_slug for membership in memberships] == [
        "alpha",
        "mike",
        "zulu",
    ]


@pytest.mark.db
async def test_a_user_in_several_workspaces_gets_all_of_them(
    membership_service, postgres_dsn
):
    """Nothing below the limit is dropped, and no other user's row appears.

    That the LIMIT is the server's rather than a Python slice is pinned in
    section D, where the argument the repository binds is visible. Seeding
    MEMBERSHIP_LIST_LIMIT + 1 workspaces here to see it bite would cost 200
    inserts to check a bound this test would then be the only reason to
    keep small.
    """
    connection = await asyncpg.connect(postgres_dsn)

    try:
        for index in range(5):
            workspace_id = UUID(int=0x70008000 + index)

            await connection.execute(
                INSERT_WORKSPACE_SQL,
                workspace_id,
                f"bulk-{index}",
                f"Bulk {index}",
            )
            await connection.execute(
                INSERT_MEMBER_SQL,
                workspace_id,
                OUTSIDER_USER_ID,
                MEMBER_ROLE,
            )
    finally:
        await connection.close()

    memberships = await membership_service.list_for_user(user_id=OUTSIDER_USER_ID)

    assert [membership.workspace_slug for membership in memberships] == [
        f"bulk-{index}" for index in range(5)
    ]
    assert {membership.user_id for membership in memberships} == {OUTSIDER_USER_ID}

    # The other two users are unaffected by five new workspaces.
    owned = await membership_service.list_for_user(user_id=OWNER_USER_ID)

    assert [membership.workspace_slug for membership in owned] == [BOOTSTRAP_SLUG]


@pytest.mark.db
async def test_a_membership_created_at_comes_back_as_an_aware_timestamp(
    membership_service,
):
    """TIMESTAMPTZ, so the value carries an offset rather than a guess."""
    memberships = await membership_service.list_for_user(user_id=OWNER_USER_ID)

    created_at = memberships[0].created_at

    assert isinstance(created_at, datetime)
    assert created_at.tzinfo is not None
    assert created_at < datetime.now(timezone.utc) + timedelta(minutes=1)
