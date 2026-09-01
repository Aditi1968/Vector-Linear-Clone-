"""Service-level pagination tests. No database involved."""

import pytest

from app.domain.errors import ValidationError
from app.domain.pagination import decode_issue_cursor, encode_issue_cursor
from app.services.issues import IssueService

from tests.conftest import (
    ExplodingPool,
    FakeIssueRepository,
    FakePool,
    make_entity,
)


def build_service(rows=None):
    pool = FakePool()
    repository = FakeIssueRepository(rows)

    return IssueService(pool=pool, repository=repository), pool, repository


@pytest.mark.parametrize("first", [0, -1, 101, 1000])
async def test_invalid_first_fails_before_pool_acquire(first):
    pool = ExplodingPool()
    repository = FakeIssueRepository()
    service = IssueService(pool=pool, repository=repository)

    with pytest.raises(ValidationError) as exc_info:
        await service.list(first=first, after=None)

    issues = exc_info.value.issues

    assert len(issues) == 1
    assert issues[0].field == "first"
    assert issues[0].code == "OUT_OF_RANGE"
    assert issues[0].message == "first must be between 1 and 100"

    assert pool.acquire_count == 0
    assert repository.list_calls == []


@pytest.mark.parametrize("first", [1, 100])
async def test_first_boundaries_are_accepted(first):
    service, pool, repository = build_service(rows=[])

    page = await service.list(first=first, after=None)

    assert page.nodes == []
    assert pool.acquire_count == 1
    assert repository.list_calls[0]["limit"] == first + 1


async def test_first_page_without_extra_row():
    rows = [make_entity(2), make_entity(1)]
    service, _, repository = build_service(rows)

    page = await service.list(first=2, after=None)

    assert len(page.nodes) == 2
    assert page.has_next_page is False

    assert repository.list_calls[0] == {
        "limit": 3,
        "after_created_at": None,
        "after_id": None,
    }

    decoded = decode_issue_cursor(page.end_cursor)
    assert decoded.id == rows[1].id
    assert decoded.created_at == rows[1].created_at


async def test_extra_row_sets_has_next_page_and_is_trimmed():
    """The +1 row drives hasNextPage but must never be returned or encoded."""
    rows = [make_entity(3), make_entity(2), make_entity(1)]
    service, _, repository = build_service(rows)

    page = await service.list(first=2, after=None)

    assert len(page.nodes) == 2
    assert page.has_next_page is True

    assert repository.list_calls[0]["limit"] == 3

    returned_ids = [node.id for node in page.nodes]
    assert returned_ids == [rows[0].id, rows[1].id]
    assert rows[2].id not in returned_ids

    # endCursor comes from the SECOND row, not the discarded third.
    decoded = decode_issue_cursor(page.end_cursor)
    assert decoded.id == rows[1].id
    assert decoded.created_at == rows[1].created_at
    assert decoded.id != rows[2].id


async def test_empty_result():
    service, _, _ = build_service(rows=[])

    page = await service.list(first=10, after=None)

    assert page.nodes == []
    assert page.has_next_page is False
    assert page.end_cursor is None


async def test_after_cursor_is_decoded_and_passed_to_repository():
    entity = make_entity(5)
    cursor = encode_issue_cursor(entity.created_at, entity.id)

    service, _, repository = build_service(rows=[make_entity(1)])

    await service.list(first=10, after=cursor)

    assert repository.list_calls[0] == {
        "limit": 11,
        "after_created_at": entity.created_at,
        "after_id": entity.id,
    }


async def test_invalid_cursor_fails_before_repository_is_called():
    pool = ExplodingPool()
    repository = FakeIssueRepository()
    service = IssueService(pool=pool, repository=repository)

    with pytest.raises(ValidationError) as exc_info:
        await service.list(first=10, after="not-a-valid-cursor")

    issues = exc_info.value.issues

    assert len(issues) == 1
    assert issues[0].field == "after"
    assert issues[0].code == "INVALID_CURSOR"
    assert issues[0].message == "Cursor is invalid"

    assert pool.acquire_count == 0
    assert repository.list_calls == []


async def test_first_and_cursor_errors_are_collected_together():
    pool = ExplodingPool()
    service = IssueService(pool=pool, repository=FakeIssueRepository())

    with pytest.raises(ValidationError) as exc_info:
        await service.list(first=0, after="bad")

    fields = [issue.field for issue in exc_info.value.issues]

    assert fields == ["first", "after"]
    assert pool.acquire_count == 0
