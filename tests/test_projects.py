"""The projects API and its validation, without a database.

Three separate things are pinned here, and the first is the one worth reading
about:

1. That the projects fields are actually ON the schema's root types. The root
   is composed in `app.graphql.schema` with `merge_types`, and a second
   `class Query(...)` written anywhere below that call does not conflict with
   it -- it silently rebinds the name, so `build_schema` picks up whichever
   definition came last and every field belonging to the classes that one does
   not inherit from vanishes from the API. That has happened in this
   repository once, and it took two fields out of the schema with the whole
   suite still green, because no test asked the schema what it exposed. These
   do.

2. That the GraphQL state enum, the domain tuple and the CHECK constraint have
   not drifted apart. `app.graphql.types.project` already raises at import if
   the first two disagree; this is what makes that guard visible as a test
   rather than as an ImportError in an unrelated file.

3. That validation refuses bad input before a connection is acquired. The pool
   used here raises on `acquire()`, so a rule that reached the database would
   fail rather than pass quietly.

The behaviour that only a server can decide -- which lead the composite key
admits, what a cross-tenant association does -- is in
tests/test_migration_009_db.py and tests/test_projects_db.py.
"""

from datetime import date
from uuid import UUID

import pytest

from app.domain.errors import ValidationError
from app.domain.patch import UNSET
from app.domain.projects import DEFAULT_PROJECT_STATE, PROJECT_STATES
from app.graphql.schema import build_schema
from app.graphql.types.project import ProjectStateType
from app.repositories.issues import IssueRepository
from app.repositories.projects import ProjectRepository
from app.services.projects import (
    DESCRIPTION_MAX_LENGTH,
    NAME_MAX_LENGTH,
    ProjectService,
)

from tests.conftest import TEST_SCOPE, ExplodingPool


PROJECT_ID = UUID("00000000-0000-7000-8000-0000000000c1")


@pytest.fixture
def schema():
    return build_schema("test")


@pytest.fixture
def service(exploding_pool: ExplodingPool) -> ProjectService:
    """The real service over a pool that refuses to open a connection."""
    return ProjectService(
        pool=exploding_pool,
        repository=ProjectRepository(),
        issue_repository=IssueRepository(),
    )


def root_fields(schema, type_name: str) -> set[str]:
    """The field names the built schema exposes on one root type.

    Read off the schema object rather than out of the SDL text, so this
    answers what the server will actually resolve.
    """
    return set(schema._schema.type_map[type_name].fields)


# ------------------------------------------------- the root types are composed


def test_the_projects_queries_reach_the_root(schema):
    """Both read fields are on Query, alongside every other feature's.

    `issues` and `me` are asserted here too, and deliberately. The failure this
    test exists for is a second root definition shadowing the merged one, and
    that failure removes OTHER features' fields rather than this one's -- so a
    test that only looked for `projects` would pass against the exact breakage
    it was written to catch.
    """
    fields = root_fields(schema, "Query")

    assert {"project", "projects"} <= fields
    assert {"issue", "issues", "me", "myWorkspaces", "teams"} <= fields


def test_the_projects_mutations_reach_the_root(schema):
    fields = root_fields(schema, "Mutation")

    assert {
        "projectCreate",
        "projectUpdate",
        "projectDelete",
        "projectTeamAdd",
        "projectTeamRemove",
        "projectMilestoneCreate",
        "projectMilestoneUpdate",
        "projectMilestoneDelete",
        "issueSetProject",
    } <= fields

    # The same argument as above: a shadowed root takes these with it.
    assert {"issueCreate", "login", "logout", "register"} <= fields


def test_the_project_type_exposes_the_lead_as_an_id(schema):
    """`leadId`, not a `lead: User`.

    `UserType` carries `email`, and today the only field returning one is
    `me` -- so the only address any caller can read is their own. A `lead:
    User` here would publish every project lead's address to everyone who can
    read the project, which is a decision about that type and not one a
    projects branch gets to make in passing.
    """
    fields = root_fields(schema, "Project")

    assert "leadId" in fields
    assert "lead" not in fields


# ----------------------------------------------------------- the state vocabulary


def test_the_graphql_enum_and_the_domain_tuple_are_the_same_states():
    """Two spellings of `projects_state_check`, pinned equal.

    `app/graphql/types/project.py` raises at import if these disagree, so this
    test can only fail by not importing -- which is the point of having it: the
    guard is stated where a reader looks for one, and the import error it
    would otherwise produce would name an unrelated module.
    """
    assert tuple(member.value for member in ProjectStateType) == PROJECT_STATES


def test_the_default_state_is_one_the_vocabulary_contains():
    assert DEFAULT_PROJECT_STATE in PROJECT_STATES
    assert ProjectStateType(DEFAULT_PROJECT_STATE) is ProjectStateType.PLANNED


# ---------------------------------------------------------------- validation


async def test_an_empty_name_is_refused_before_a_connection_is_taken(
    service, exploding_pool
):
    with pytest.raises(ValidationError) as raised:
        await service.create(
            scope=TEST_SCOPE,
            name="",
            description=None,
            state=DEFAULT_PROJECT_STATE,
            target_date=None,
        )

    assert [(issue.field, issue.code) for issue in raised.value.issues] == [
        ("name", "REQUIRED")
    ]
    assert exploding_pool.acquire_count == 0


async def test_an_over_long_name_is_refused(service, exploding_pool):
    with pytest.raises(ValidationError) as raised:
        await service.create(
            scope=TEST_SCOPE,
            name="x" * (NAME_MAX_LENGTH + 1),
            description=None,
            state=DEFAULT_PROJECT_STATE,
            target_date=None,
        )

    assert [(issue.field, issue.code) for issue in raised.value.issues] == [
        ("name", "TOO_LONG")
    ]
    assert exploding_pool.acquire_count == 0


async def test_an_unknown_state_is_refused_here_rather_than_by_the_constraint(
    service, exploding_pool
):
    """The service names the legal states; the CHECK would only say no.

    `projects_state_check` rejects this too, but as a CheckViolationError
    carrying the rendered constraint -- which is either masked (telling the
    client nothing) or forwarded (telling it about the schema). Neither answers
    "which states may I send".
    """
    with pytest.raises(ValidationError) as raised:
        await service.create(
            scope=TEST_SCOPE,
            name="Launch",
            description=None,
            state="shipped",
            target_date=None,
        )

    issue = raised.value.issues[0]

    assert (issue.field, issue.code) == ("state", "INVALID")
    assert all(state in issue.message for state in PROJECT_STATES)
    assert exploding_pool.acquire_count == 0


async def test_every_violation_is_reported_at_once(service, exploding_pool):
    """One round of corrections, not one field per attempt."""
    with pytest.raises(ValidationError) as raised:
        await service.create(
            scope=TEST_SCOPE,
            name="",
            description="d" * (DESCRIPTION_MAX_LENGTH + 1),
            state="shipped",
            target_date=None,
        )

    assert [issue.field for issue in raised.value.issues] == [
        "name",
        "description",
        "state",
    ]
    assert exploding_pool.acquire_count == 0


async def test_a_field_the_patch_does_not_mention_is_not_validated(
    service, exploding_pool
):
    """UNSET short-circuits each check rather than being coerced to a value.

    An empty name is a REQUIRED error; an *absent* name is not an error at
    all, and a service that could not tell them apart would refuse every patch
    that only changed the state.
    """
    with pytest.raises(ValidationError) as raised:
        await service.update(
            scope=TEST_SCOPE,
            project_id=PROJECT_ID,
            state="shipped",
        )

    assert [issue.field for issue in raised.value.issues] == ["state"]
    assert exploding_pool.acquire_count == 0


async def test_a_patch_that_sets_nothing_at_all_still_reaches_the_database(
    service, exploding_pool
):
    """The empty patch is a read, not a no-op decided in Python.

    It has to return the project as it stands, and whether there IS one with
    that id in this workspace is a question only the database answers -- so
    this acquires a connection, and the exploding pool is what proves it does.
    A version that returned early would answer NOT_FOUND for a real project.
    """
    with pytest.raises(AssertionError, match="must not be called"):
        await service.update(scope=TEST_SCOPE, project_id=PROJECT_ID)

    assert exploding_pool.acquire_count == 1


async def test_a_lead_is_never_validated_in_the_service(service, exploding_pool):
    """Whether a lead is a member is not a question this process may answer.

    A well-formed id gets no pre-flight lookup: validation passes and the
    connection is taken, where `projects_lead_fk` decides. A SELECT-first check
    would be a second, weaker copy of that constraint -- weaker because the
    membership can be revoked between the check and the insert.
    """
    with pytest.raises(AssertionError, match="must not be called"):
        await service.create(
            scope=TEST_SCOPE,
            name="Launch",
            description=None,
            state=DEFAULT_PROJECT_STATE,
            target_date=date(2026, 6, 1),
            lead_id=UUID("00000000-0000-7000-8000-0000000000ff"),
        )

    assert exploding_pool.acquire_count == 1


async def test_a_negative_milestone_position_is_refused(service, exploding_pool):
    with pytest.raises(ValidationError) as raised:
        await service.update_milestone(
            scope=TEST_SCOPE,
            milestone_id=PROJECT_ID,
            name=UNSET,
            position=-1,
        )

    assert [(issue.field, issue.code) for issue in raised.value.issues] == [
        ("position", "OUT_OF_RANGE")
    ]
    assert exploding_pool.acquire_count == 0
