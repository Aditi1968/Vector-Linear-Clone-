"""Repository SQL tests against a fake asyncpg connection."""

from app.domain.issues import IssueEntity
from app.repositories.issues import IssueRepository

from tests.conftest import (
    TEST_SCOPE,
    TEST_WORKSPACE_ID,
    FakeConnection,
    as_record,
    make_entity,
    normalize,
)


async def test_no_cursor_page_is_still_scoped_to_one_workspace():
    """There is no unscoped listing query, cursor or not.

    The first page is the easy one to get wrong: with no cursor there is
    nothing else in the WHERE clause, so an implementation that forgot the
    tenant here produces a syntactically clean statement that reads every
    issue in the product.
    """
    connection = FakeConnection(rows=[])
    repository = IssueRepository()

    await repository.list(
        connection,
        scope=TEST_SCOPE,
        limit=51,
        after_created_at=None,
        after_id=None,
    )

    query = normalize(connection.queries[0]["query"])

    assert "WHERE workspace_id = $1" in query
    assert "ORDER BY created_at DESC, id DESC" in query
    assert "LIMIT $2" in query
    assert "OFFSET" not in query

    # Values are bound as parameters, never interpolated into the SQL.
    assert connection.queries[0]["args"] == (TEST_WORKSPACE_ID, 51)
    assert str(TEST_WORKSPACE_ID) not in query


async def test_cursor_path_uses_row_value_comparison_inside_the_workspace():
    """The tenant is an equality; only (created_at, id) is the keyset.

    Widening the row-value comparison to include `workspace_id` would put
    workspaces into the ordering, and a walk that reached the end of one
    tenant would carry on into the next.
    """
    entity = make_entity(3)
    connection = FakeConnection(rows=[])
    repository = IssueRepository()

    await repository.list(
        connection,
        scope=TEST_SCOPE,
        limit=11,
        after_created_at=entity.created_at,
        after_id=entity.id,
    )

    query = normalize(connection.queries[0]["query"])

    assert "WHERE workspace_id = $1 AND (created_at, id) < ($2, $3)" in query
    assert "ORDER BY created_at DESC, id DESC" in query
    assert "LIMIT $4" in query
    assert "OFFSET" not in query

    # Values are bound as parameters, never interpolated into the SQL.
    assert connection.queries[0]["args"] == (
        TEST_WORKSPACE_ID,
        entity.created_at,
        entity.id,
        11,
    )
    assert str(entity.id) not in query
    assert str(TEST_WORKSPACE_ID) not in query


async def test_records_are_converted_to_entities():
    entities = [make_entity(2), make_entity(1)]
    connection = FakeConnection(rows=[as_record(e) for e in entities])
    repository = IssueRepository()

    result = await repository.list(
        connection,
        scope=TEST_SCOPE,
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
        scope=TEST_SCOPE,
        limit=50 + 1,
        after_created_at=None,
        after_id=None,
    )

    assert connection.queries[0]["args"] == (TEST_WORKSPACE_ID, 51)
