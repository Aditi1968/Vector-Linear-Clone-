"""Repository write-path SQL tests against a fake asyncpg connection."""

from uuid import UUID

from app.domain.issues import IssueEntity
from app.repositories.issues import IssueRepository

from tests.conftest import (
    TEST_SCOPE,
    TEST_TEAM_ID,
    TEST_WORKSPACE_ID,
    FakeConnection,
    as_record,
    make_entity,
    normalize,
)


ENTITY_COLUMNS = (
    "id",
    "title",
    "description",
    "priority",
    "cycle_id",
    "completed_at",
    "created_at",
    "updated_at",
)


def inserted_columns(query: str) -> list[str]:
    names = query.split("INSERT INTO issues (")[1].split(")")[0]

    return [name.strip() for name in names.split(",")]


TEST_NUMBER = 7
TEST_WORKFLOW_STATE_ID = UUID("00000000-0000-7000-8000-0000000000e9")


async def test_create_inserts_the_tenancy_columns_and_nothing_generated():
    """id and the timestamps are the database's to assign; the tenant is ours.

    `workspace_id` and `team_id` carry no database default on purpose, so an
    insert that omitted either would be a NOT NULL violation rather than a
    row quietly filed in the bootstrap tenant. Naming the exact column list
    is what keeps that: a fourth column appearing here, or one of these two
    disappearing, fails.
    """
    entity = make_entity(1)
    connection = FakeConnection(row=as_record(entity))

    await IssueRepository().create(
        connection,
        scope=TEST_SCOPE,
        team_id=TEST_TEAM_ID,
        number=TEST_NUMBER,
        workflow_state_id=TEST_WORKFLOW_STATE_ID,
        title=entity.title,
        description="a description",
        priority=3,
    )

    query = normalize(connection.queries[0]["query"])

    assert inserted_columns(query) == [
        "workspace_id",
        "team_id",
        "number",
        "workflow_state_id",
        "title",
        "description",
        "priority",
    ]
    assert "VALUES ($1, $2, $3, $4, $5, $6, $7)" in query


async def test_create_binds_values_in_declared_column_order():
    entity = make_entity(1)
    connection = FakeConnection(row=as_record(entity))

    await IssueRepository().create(
        connection,
        scope=TEST_SCOPE,
        team_id=TEST_TEAM_ID,
        number=TEST_NUMBER,
        workflow_state_id=TEST_WORKFLOW_STATE_ID,
        title="Ship it",
        description="a description",
        priority=3,
    )

    query = normalize(connection.queries[0]["query"])

    # Values are bound as parameters, never interpolated into the SQL.
    assert connection.queries[0]["args"] == (
        TEST_WORKSPACE_ID,
        TEST_TEAM_ID,
        TEST_NUMBER,
        TEST_WORKFLOW_STATE_ID,
        "Ship it",
        "a description",
        3,
    )
    assert "Ship it" not in query
    assert "a description" not in query
    assert str(TEST_WORKSPACE_ID) not in query


async def test_create_writes_the_workspace_it_was_given_not_the_team_s():
    """The two ids are distinct arguments and must not be transposed.

    Both are UUIDs, so a swap is invisible to every type and, against a
    single-team bootstrap tenant, would still insert successfully often
    enough to look fine.
    """
    entity = make_entity(1)
    connection = FakeConnection(row=as_record(entity))

    await IssueRepository().create(
        connection,
        scope=TEST_SCOPE,
        team_id=TEST_TEAM_ID,
        number=TEST_NUMBER,
        workflow_state_id=TEST_WORKFLOW_STATE_ID,
        title="Ship it",
        description=None,
        priority=0,
    )

    workspace_argument, team_argument = connection.queries[0]["args"][:2]

    assert workspace_argument == TEST_WORKSPACE_ID
    assert team_argument == TEST_TEAM_ID
    assert workspace_argument != team_argument


async def test_create_returns_every_column_the_entity_needs():
    """A short RETURNING list would surface as a KeyError at row mapping."""
    entity = make_entity(1)
    connection = FakeConnection(row=as_record(entity))

    await IssueRepository().create(
        connection,
        scope=TEST_SCOPE,
        team_id=TEST_TEAM_ID,
        number=TEST_NUMBER,
        workflow_state_id=TEST_WORKFLOW_STATE_ID,
        title=entity.title,
        description=None,
        priority=entity.priority,
    )

    returning = normalize(connection.queries[0]["query"]).split("RETURNING")[1]

    for column in ENTITY_COLUMNS:
        assert column in returning


async def test_create_maps_the_returned_row_onto_the_entity():
    entity = make_entity(4)
    connection = FakeConnection(row=as_record(entity))

    result = await IssueRepository().create(
        connection,
        scope=TEST_SCOPE,
        team_id=TEST_TEAM_ID,
        number=TEST_NUMBER,
        workflow_state_id=TEST_WORKFLOW_STATE_ID,
        title=entity.title,
        description=entity.description,
        priority=entity.priority,
    )

    assert result == entity
    assert isinstance(result, IssueEntity)


async def test_create_with_null_description_still_binds_every_parameter():
    """A None description is a bound NULL, not an omitted parameter."""
    entity = make_entity(1)
    connection = FakeConnection(row=as_record(entity))

    await IssueRepository().create(
        connection,
        scope=TEST_SCOPE,
        team_id=TEST_TEAM_ID,
        number=TEST_NUMBER,
        workflow_state_id=TEST_WORKFLOW_STATE_ID,
        title="Ship it",
        description=None,
        priority=0,
    )

    assert connection.queries[0]["args"] == (
        TEST_WORKSPACE_ID,
        TEST_TEAM_ID,
        TEST_NUMBER,
        TEST_WORKFLOW_STATE_ID,
        "Ship it",
        None,
        0,
    )
    assert "$5" in normalize(connection.queries[0]["query"])


async def test_get_by_id_filters_on_the_workspace_as_well_as_the_key():
    """The tenant is in the WHERE clause, not in a check after the fetch.

    A lookup on the primary key alone would return another tenant's row into
    this process and leave "is it mine" to whatever the caller remembers to
    do next.
    """
    entity = make_entity(2)
    connection = FakeConnection(row=as_record(entity))

    await IssueRepository().get_by_id(
        connection,
        scope=TEST_SCOPE,
        issue_id=entity.id,
    )

    query = normalize(connection.queries[0]["query"])

    assert "WHERE workspace_id = $1 AND id = $2" in query

    # Values are bound as parameters, never interpolated into the SQL.
    assert connection.queries[0]["args"] == (TEST_WORKSPACE_ID, entity.id)
    assert str(entity.id) not in query
    assert str(TEST_WORKSPACE_ID) not in query


async def test_get_by_id_maps_the_row_onto_the_entity():
    entity = make_entity(2)
    connection = FakeConnection(row=as_record(entity))

    result = await IssueRepository().get_by_id(
        connection,
        scope=TEST_SCOPE,
        issue_id=entity.id,
    )

    assert result == entity
    assert isinstance(result, IssueEntity)


async def test_get_by_id_returns_none_for_an_absent_row():
    """A missing issue is an ordinary answer; only the caller decides it is a 404."""
    connection = FakeConnection(row=None)

    result = await IssueRepository().get_by_id(
        connection,
        scope=TEST_SCOPE,
        issue_id=UUID(int=99),
    )

    assert result is None


async def test_get_by_id_selects_every_column_the_entity_needs():
    connection = FakeConnection(row=None)

    await IssueRepository().get_by_id(
        connection,
        scope=TEST_SCOPE,
        issue_id=UUID(int=99),
    )

    query = normalize(connection.queries[0]["query"])

    for column in ENTITY_COLUMNS:
        assert column in query
