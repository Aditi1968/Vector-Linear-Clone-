"""Repository write-path SQL tests against a fake asyncpg connection."""

from uuid import UUID

from app.domain.issues import IssueEntity
from app.repositories.issues import IssueRepository

from tests.conftest import FakeConnection, as_record, make_entity, normalize


ENTITY_COLUMNS = (
    "id",
    "title",
    "description",
    "priority",
    "completed_at",
    "created_at",
    "updated_at",
)


def inserted_columns(query: str) -> list[str]:
    names = query.split("INSERT INTO issues (")[1].split(")")[0]

    return [name.strip() for name in names.split(",")]


async def test_create_inserts_only_the_caller_supplied_columns():
    """id and the timestamps are the database's to assign, not ours."""
    entity = make_entity(1)
    connection = FakeConnection(row=as_record(entity))

    await IssueRepository().create(
        connection,
        title=entity.title,
        description="a description",
        priority=3,
    )

    query = normalize(connection.queries[0]["query"])

    assert inserted_columns(query) == ["title", "description", "priority"]
    assert "VALUES ($1, $2, $3)" in query


async def test_create_binds_values_in_declared_column_order():
    entity = make_entity(1)
    connection = FakeConnection(row=as_record(entity))

    await IssueRepository().create(
        connection,
        title="Ship it",
        description="a description",
        priority=3,
    )

    query = normalize(connection.queries[0]["query"])

    # Values are bound as parameters, never interpolated into the SQL.
    assert connection.queries[0]["args"] == ("Ship it", "a description", 3)
    assert "Ship it" not in query
    assert "a description" not in query


async def test_create_returns_every_column_the_entity_needs():
    """A short RETURNING list would surface as a KeyError at row mapping."""
    entity = make_entity(1)
    connection = FakeConnection(row=as_record(entity))

    await IssueRepository().create(
        connection,
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
        title=entity.title,
        description=entity.description,
        priority=entity.priority,
    )

    assert result == entity
    assert isinstance(result, IssueEntity)


async def test_create_with_null_description_still_binds_three_parameters():
    """A None description is a bound NULL, not an omitted parameter."""
    entity = make_entity(1)
    connection = FakeConnection(row=as_record(entity))

    await IssueRepository().create(
        connection,
        title="Ship it",
        description=None,
        priority=0,
    )

    assert connection.queries[0]["args"] == ("Ship it", None, 0)
    assert "$3" in normalize(connection.queries[0]["query"])


async def test_get_by_id_filters_on_the_primary_key():
    entity = make_entity(2)
    connection = FakeConnection(row=as_record(entity))

    await IssueRepository().get_by_id(connection, entity.id)

    query = normalize(connection.queries[0]["query"])

    assert "WHERE id = $1" in query

    # Values are bound as parameters, never interpolated into the SQL.
    assert connection.queries[0]["args"] == (entity.id,)
    assert str(entity.id) not in query


async def test_get_by_id_maps_the_row_onto_the_entity():
    entity = make_entity(2)
    connection = FakeConnection(row=as_record(entity))

    result = await IssueRepository().get_by_id(connection, entity.id)

    assert result == entity
    assert isinstance(result, IssueEntity)


async def test_get_by_id_returns_none_for_an_absent_row():
    """A missing issue is an ordinary answer; only the caller decides it is a 404."""
    connection = FakeConnection(row=None)

    result = await IssueRepository().get_by_id(connection, UUID(int=99))

    assert result is None


async def test_get_by_id_selects_every_column_the_entity_needs():
    connection = FakeConnection(row=None)

    await IssueRepository().get_by_id(connection, UUID(int=99))

    query = normalize(connection.queries[0]["query"])

    for column in ENTITY_COLUMNS:
        assert column in query
