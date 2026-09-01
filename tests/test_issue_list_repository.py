"""Repository SQL tests against a fake asyncpg connection."""

from app.domain.issues import IssueEntity
from app.repositories.issues import IssueRepository

from tests.conftest import FakeConnection, make_entity


def as_record(entity: IssueEntity) -> dict:
    """asyncpg.Record supports __getitem__, which a dict models well enough."""
    return {
        "id": entity.id,
        "title": entity.title,
        "description": entity.description,
        "priority": entity.priority,
        "completed_at": entity.completed_at,
        "created_at": entity.created_at,
        "updated_at": entity.updated_at,
    }


def normalize(sql: str) -> str:
    return " ".join(sql.split())


async def test_no_cursor_uses_query_without_where_clause():
    connection = FakeConnection(rows=[])
    repository = IssueRepository()

    await repository.list(
        connection,
        limit=51,
        after_created_at=None,
        after_id=None,
    )

    query = normalize(connection.queries[0]["query"])

    assert "WHERE" not in query
    assert "ORDER BY created_at DESC, id DESC" in query
    assert "LIMIT $1" in query
    assert "OFFSET" not in query

    assert connection.queries[0]["args"] == (51,)


async def test_cursor_path_uses_row_value_comparison():
    entity = make_entity(3)
    connection = FakeConnection(rows=[])
    repository = IssueRepository()

    await repository.list(
        connection,
        limit=11,
        after_created_at=entity.created_at,
        after_id=entity.id,
    )

    query = normalize(connection.queries[0]["query"])

    assert "WHERE (created_at, id) < ($1, $2)" in query
    assert "ORDER BY created_at DESC, id DESC" in query
    assert "LIMIT $3" in query
    assert "OFFSET" not in query

    # Values are bound as parameters, never interpolated into the SQL.
    assert connection.queries[0]["args"] == (entity.created_at, entity.id, 11)
    assert str(entity.id) not in query


async def test_records_are_converted_to_entities():
    entities = [make_entity(2), make_entity(1)]
    connection = FakeConnection(rows=[as_record(e) for e in entities])
    repository = IssueRepository()

    result = await repository.list(
        connection,
        limit=3,
        after_created_at=None,
        after_id=None,
    )

    assert result == entities
    assert all(isinstance(item, IssueEntity) for item in result)


async def test_limit_receives_first_plus_one_from_service():
    """The repository is handed first+1 verbatim; it does not adjust it."""
    connection = FakeConnection(rows=[])

    await IssueRepository().list(
        connection,
        limit=50 + 1,
        after_created_at=None,
        after_id=None,
    )

    assert connection.queries[0]["args"] == (51,)
