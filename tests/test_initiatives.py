"""The initiatives API and its validation, without a database.

Three separate things are pinned here, and the first is the one worth reading
about:

1. That the initiative fields are actually ON the schema's root types. The root
   is composed in `app.graphql.schema` with `merge_types`, and a second
   `class Query(...)` written anywhere below that call does not conflict with
   it -- it silently rebinds the name, so `build_schema` picks up whichever
   definition came last and every field belonging to the classes that one does
   not inherit from vanishes from the API. That has happened in this repository
   once, and it took two fields out of the schema with the whole suite still
   green, because no test asked the schema what it exposed. These do.

2. That the two GraphQL enums, the two domain tuples and the four CHECK
   constraints have not drifted apart. `app.graphql.types.initiative` and
   `app.graphql.types.health` already raise at import if the first pairs
   disagree; this is what makes those guards visible as tests rather than as an
   ImportError in an unrelated file. The CHECK half is
   tests/test_migration_022_db.py, which reads the constraints back out of the
   catalog.

3. That validation refuses bad input before a connection is acquired. The pool
   used here raises on `acquire()`, so a rule that reached the database would
   fail rather than pass quietly.

The behaviour that only a server can decide -- what the composite keys admit,
whether a re-parent closes a loop, how deep a tree may go -- is in
tests/test_migration_022_db.py and tests/test_initiatives_db.py. The cycle and
depth guards in particular CANNOT be tested here: they are recursive queries,
so a fake connection would be asserting on the shape of a string rather than on
what PostgreSQL does with it.
"""

from uuid import UUID

import pytest

from app.domain.errors import ValidationError
from app.domain.health import HEALTH_VALUES
from app.domain.initiatives import (
    DEFAULT_INITIATIVE_STATUS,
    INITIATIVE_STATUSES,
    MAX_INITIATIVE_DEPTH,
)
from app.graphql.schema import build_schema
from app.graphql.types.health import HealthType
from app.graphql.types.initiative import InitiativeStatusType
from app.repositories.initiatives import (
    INITIATIVE_PARENTING_LOCK_CLASS,
    InitiativeRepository,
)
from app.repositories.projects import DEPENDENCY_LOCK_CLASS
from app.repositories.relations import PARENTING_LOCK_CLASS
from app.services.initiatives import (
    DESCRIPTION_MAX_LENGTH,
    NAME_MAX_LENGTH,
    UPDATE_BODY_MAX_LENGTH,
    InitiativeService,
)

from tests.conftest import TEST_SCOPE, ExplodingPool


INITIATIVE_ID = UUID("00000000-0000-7000-8000-0000000000d1")
MEMBER_ID = UUID("00000000-0000-7000-8000-0000000000e1")


@pytest.fixture
def schema():
    return build_schema("test")


@pytest.fixture
def service(exploding_pool: ExplodingPool) -> InitiativeService:
    """The real service over a pool that refuses to open a connection."""
    return InitiativeService(pool=exploding_pool, repository=InitiativeRepository())


def root_fields(schema, type_name: str) -> set[str]:
    """The field names the built schema exposes on one type.

    Read off the schema object rather than out of the SDL text, so this answers
    what the server will actually resolve.
    """
    return set(schema._schema.type_map[type_name].fields)


def issues(raised) -> list[tuple[str, str]]:
    return [(issue.field, issue.code) for issue in raised.value.issues]


# ------------------------------------------------- the root types are composed


def test_the_initiative_queries_reach_the_root(schema):
    """Both read fields are on Query, alongside every other feature's.

    `issues` and `me` are asserted here too, and deliberately. The failure this
    test exists for is a second root definition shadowing the merged one, and
    that failure removes OTHER features' fields rather than this one's -- so a
    test that only looked for `initiatives` would pass against the exact
    breakage it was written to catch.
    """
    fields = root_fields(schema, "Query")

    assert {"initiative", "initiatives"} <= fields
    assert {"issue", "issues", "me", "project", "projects"} <= fields


def test_the_initiative_mutations_reach_the_root(schema):
    fields = root_fields(schema, "Mutation")

    assert {
        "initiativeCreate",
        "initiativeUpdate",
        "initiativeDelete",
        "initiativeProjectAdd",
        "initiativeProjectRemove",
        "initiativeSetParent",
        "initiativeClearParent",
        "initiativeUpdatePost",
    } <= fields

    # The same argument as above: a shadowed root takes these with it.
    assert {"issueCreate", "projectCreate", "login"} <= fields


def test_the_project_health_mutations_reach_the_root(schema):
    fields = root_fields(schema, "Mutation")

    assert {
        "projectUpdatePost",
        "projectDependencyAdd",
        "projectDependencyRemove",
    } <= fields


def test_the_initiative_type_exposes_its_edges_as_ids(schema):
    """`projectIds` and `childInitiativeIds`, not resolved objects.

    Two reasons, and the second is the load-bearing one. A field named for what
    it holds can gain a resolved sibling later without changing type, which a
    field called `projects` returning ids could not. And a resolved list here
    would be a fan-out `app/graphql/limits.py` prices as one, because it
    declares no page-size argument -- so `initiatives(first: 100) { projects }`
    would be a hundred reads inside one budgeted field.
    """
    fields = root_fields(schema, "Initiative")

    assert {"projectIds", "childInitiativeIds", "ownerId"} <= fields
    assert not {"projects", "childInitiatives", "owner"} & fields


def test_the_project_type_gained_health_updates_and_dependencies(schema):
    fields = root_fields(schema, "Project")

    assert {"health", "updates", "dependencies"} <= fields


def test_an_update_exposes_its_author_as_an_id(schema):
    """`authorId`, not `author: User`, for the reason `Project.leadId` gives:
    `UserType` carries `email`, so a resolved author would publish every update
    author's address to everyone who can read the project."""
    for type_name in ("ProjectUpdate", "InitiativeUpdate"):
        fields = root_fields(schema, type_name)

        assert "authorId" in fields
        assert "author" not in fields


def test_neither_update_input_accepts_an_author(schema):
    """The author is the session's user and never an argument.

    A `authorId` input field would be an impersonation API: anyone who can post
    an update could post one as anybody. The composite author key refuses a
    non-member, but it cannot tell one member from another -- so this is the
    only place the rule can live, and it lives in the shape of the schema
    rather than in a resolver remembering.
    """
    for type_name in ("ProjectUpdatePostInput", "InitiativeUpdatePostInput"):
        assert "authorId" not in root_fields(schema, type_name)


def test_initiative_create_does_not_accept_a_parent(schema):
    """Nesting is `initiativeSetParent` and nothing else.

    That mutation is the only path that takes the lock and runs the cycle and
    depth walk. An `initiativeCreate` accepting a parent would be a second
    writer of the column outside that guard -- which is exactly how
    migrations/022_initiatives.sql says the guarantee gets lost with no failing
    test to say so. This is that failing test.
    """
    fields = root_fields(schema, "InitiativeCreateInput")

    assert "parentInitiativeId" not in fields
    # And no health either: health arrives by posting an update, so that every
    # value a board renders has a body and an author behind it.
    assert "health" not in fields


def test_initiative_update_cannot_set_the_health_or_the_parent(schema):
    fields = root_fields(schema, "InitiativeUpdateInput")

    assert not {"health", "parentInitiativeId"} & fields


# ----------------------------------------------------------- the vocabularies


def test_the_status_enum_and_the_domain_tuple_are_the_same_statuses():
    """Two spellings of `initiatives_status_check`, pinned equal.

    `app/graphql/types/initiative.py` raises at import if these disagree, so
    this test can only fail by not importing -- which is the point of having
    it: the guard is stated where a reader looks for one, and the import error
    it would otherwise produce would name an unrelated module.
    """
    assert tuple(member.value for member in InitiativeStatusType) == INITIATIVE_STATUSES


def test_the_health_enum_and_the_domain_tuple_are_the_same_values():
    assert tuple(member.value for member in HealthType) == HEALTH_VALUES


def test_the_default_status_is_one_the_vocabulary_contains():
    assert DEFAULT_INITIATIVE_STATUS in INITIATIVE_STATUSES
    assert (
        InitiativeStatusType(DEFAULT_INITIATIVE_STATUS) is InitiativeStatusType.PLANNED
    )


def test_health_is_one_enum_shared_by_both_features(schema):
    """One type, not a ProjectHealth and an InitiativeHealth.

    Two enums differing only in name would be two types a client has to map
    between to render one badge, and two places for a fourth value to be added
    to one of. Asserted against the built schema rather than against the Python
    class, because the thing that could go wrong is a second `@strawberry.enum`
    named differently.
    """
    assert set(schema._schema.type_map["Health"].values) == {
        "ON_TRACK",
        "AT_RISK",
        "OFF_TRACK",
    }
    assert "ProjectHealth" not in schema._schema.type_map
    assert "InitiativeHealth" not in schema._schema.type_map


def test_the_three_advisory_lock_classes_are_distinct():
    """Three graphs, three locks.

    Sub-issue parenting, initiative parenting and project dependencies each
    serialise their own writers. Sharing a number would be correct but would
    make every re-parent in a workspace queue behind every dependency edit --
    and, worse, would make it non-obvious which guard a future fourth graph was
    accidentally joining.
    """
    assert (
        len(
            {
                PARENTING_LOCK_CLASS,
                INITIATIVE_PARENTING_LOCK_CLASS,
                DEPENDENCY_LOCK_CLASS,
            }
        )
        == 3
    )


def test_the_depth_bound_leaves_room_for_a_real_hierarchy():
    """A bound low enough to be meaningless is a bound nobody can ship under.

    Not an arbitrary assertion: the number is what caps the recursive walk in
    `inspect_parenting`, so lowering it is a performance decision AND a product
    one, and this is the line that makes the second half visible.
    """
    assert MAX_INITIATIVE_DEPTH >= 2


# ---------------------------------------------------------------- validation


async def test_an_empty_name_is_refused_before_a_connection_is_taken(
    service, exploding_pool
):
    with pytest.raises(ValidationError) as raised:
        await service.create(
            scope=TEST_SCOPE,
            name="",
            description=None,
            status=DEFAULT_INITIATIVE_STATUS,
            target_date=None,
        )

    assert issues(raised) == [("name", "REQUIRED")]
    assert exploding_pool.acquire_count == 0


async def test_an_over_long_name_is_refused(service, exploding_pool):
    with pytest.raises(ValidationError) as raised:
        await service.create(
            scope=TEST_SCOPE,
            name="x" * (NAME_MAX_LENGTH + 1),
            description=None,
            status=DEFAULT_INITIATIVE_STATUS,
            target_date=None,
        )

    assert issues(raised) == [("name", "TOO_LONG")]
    assert exploding_pool.acquire_count == 0


async def test_an_unknown_status_is_refused_here_rather_than_by_the_constraint(
    service, exploding_pool
):
    """The service names the legal statuses; the CHECK would only say no.

    `initiatives_status_check` rejects this too, but as a CheckViolationError
    carrying the rendered constraint -- which is either masked (telling the
    client nothing) or forwarded (telling it about the schema). Neither answers
    "which statuses may I send".
    """
    with pytest.raises(ValidationError) as raised:
        await service.create(
            scope=TEST_SCOPE,
            name="Ship v2",
            description=None,
            status="shipped",
            target_date=None,
        )

    issue = raised.value.issues[0]

    assert (issue.field, issue.code) == ("status", "INVALID")
    assert all(status in issue.message for status in INITIATIVE_STATUSES)
    assert exploding_pool.acquire_count == 0


async def test_every_violation_is_reported_at_once(service, exploding_pool):
    """One round of corrections, not one field per attempt."""
    with pytest.raises(ValidationError) as raised:
        await service.create(
            scope=TEST_SCOPE,
            name="",
            description="d" * (DESCRIPTION_MAX_LENGTH + 1),
            status="shipped",
            target_date=None,
        )

    assert [field for field, _ in issues(raised)] == [
        "name",
        "description",
        "status",
    ]
    assert exploding_pool.acquire_count == 0


async def test_a_field_the_patch_does_not_mention_is_not_validated(
    service, exploding_pool
):
    """UNSET short-circuits each check rather than being coerced to a value.

    An empty name is a REQUIRED error; an *absent* name is not an error at all,
    and a service that could not tell them apart would refuse every patch that
    only changed the status.
    """
    with pytest.raises(ValidationError) as raised:
        await service.update(
            scope=TEST_SCOPE,
            initiative_id=INITIATIVE_ID,
            status="shipped",
        )

    assert [field for field, _ in issues(raised)] == ["status"]
    assert exploding_pool.acquire_count == 0


async def test_a_patch_that_sets_nothing_at_all_still_reaches_the_database(
    service, exploding_pool
):
    """The empty patch is a read, not a no-op decided in Python.

    It has to return the initiative as it stands, and whether there IS one with
    that id in this workspace is a question only the database answers -- so
    this acquires a connection, and the exploding pool is what proves it does.
    A version that returned early would answer NOT_FOUND for a real initiative.
    """
    with pytest.raises(AssertionError, match="must not be called"):
        await service.update(scope=TEST_SCOPE, initiative_id=INITIATIVE_ID)

    assert exploding_pool.acquire_count == 1


async def test_an_owner_is_never_validated_in_the_service(service, exploding_pool):
    """Whether an owner is a member is not a question this process may answer.

    A well-formed id gets no pre-flight lookup: validation passes and the
    connection is taken, where `initiatives_owner_fk` decides. A SELECT-first
    check would be a second, weaker copy of that constraint -- weaker because
    the membership can be revoked between the check and the insert.
    """
    with pytest.raises(AssertionError, match="must not be called"):
        await service.create(
            scope=TEST_SCOPE,
            name="Ship v2",
            description=None,
            status=DEFAULT_INITIATIVE_STATUS,
            target_date=None,
            owner_id=MEMBER_ID,
        )

    assert exploding_pool.acquire_count == 1


async def test_an_unknown_health_is_refused_before_a_connection_is_taken(
    service, exploding_pool
):
    with pytest.raises(ValidationError) as raised:
        await service.post_update(
            scope=TEST_SCOPE,
            initiative_id=INITIATIVE_ID,
            health="probably_fine",
            body="Should be OK.",
            author_id=MEMBER_ID,
        )

    issue = raised.value.issues[0]

    assert (issue.field, issue.code) == ("health", "INVALID")
    assert all(value in issue.message for value in HEALTH_VALUES)
    assert exploding_pool.acquire_count == 0


async def test_an_empty_update_body_is_refused(service, exploding_pool):
    with pytest.raises(ValidationError) as raised:
        await service.post_update(
            scope=TEST_SCOPE,
            initiative_id=INITIATIVE_ID,
            health="on_track",
            body="",
            author_id=MEMBER_ID,
        )

    assert issues(raised) == [("body", "REQUIRED")]
    assert exploding_pool.acquire_count == 0


async def test_an_over_long_update_body_is_refused(service, exploding_pool):
    """The service's bound and `initiative_updates_body_length` are two
    statements of one rule. Refusing here is what turns a CheckViolationError
    -- masked, and therefore useless to a client -- into a field error."""
    with pytest.raises(ValidationError) as raised:
        await service.post_update(
            scope=TEST_SCOPE,
            initiative_id=INITIATIVE_ID,
            health="on_track",
            body="b" * (UPDATE_BODY_MAX_LENGTH + 1),
            author_id=MEMBER_ID,
        )

    assert issues(raised) == [("body", "TOO_LONG")]
    assert exploding_pool.acquire_count == 0


async def test_self_parenting_is_refused_before_a_connection_is_taken(
    service, exploding_pool
):
    """A comparison of two arguments reads no row, so it needs no server.

    The multi-step cycle guard DOES need one and is in
    tests/test_initiatives_db.py; this is the one case that can be settled
    here, and settling it here is what keeps the obvious mistake from costing a
    lock and a recursive query.
    """
    with pytest.raises(ValidationError) as raised:
        await service.set_parent(
            scope=TEST_SCOPE,
            initiative_id=INITIATIVE_ID,
            parent_id=INITIATIVE_ID,
        )

    assert issues(raised) == [("parentInitiativeId", "SELF_PARENT")]
    assert exploding_pool.acquire_count == 0


@pytest.mark.parametrize("first", [0, 101])
async def test_an_out_of_range_page_size_is_refused(service, exploding_pool, first):
    """`first` is never silently clamped, and the contract is IssueService's
    and ProjectService's -- three list endpoints that disagreed about the legal
    page size would be a contract a client has to learn three times."""
    with pytest.raises(ValidationError) as raised:
        await service.list(scope=TEST_SCOPE, first=first, after=None)

    assert issues(raised) == [("first", "OUT_OF_RANGE")]
    assert exploding_pool.acquire_count == 0


async def test_an_unreadable_cursor_is_an_input_error_and_not_a_crash(
    service, exploding_pool
):
    with pytest.raises(ValidationError) as raised:
        await service.list(scope=TEST_SCOPE, first=50, after="not-a-cursor")

    assert issues(raised) == [("after", "INVALID_CURSOR")]
    assert exploding_pool.acquire_count == 0
