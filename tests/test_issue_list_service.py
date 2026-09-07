"""Service-level pagination tests. No database involved."""

from uuid import UUID

import pytest

from app.domain.errors import ValidationError
from app.domain.issues import (
    NO_FILTER,
    IssueFilter,
    IssueOrder,
    IssueOrderField,
    OrderDirection,
)
from app.domain.pagination import (
    IssueListCursor,
    decode_issue_list_cursor,
    encode_issue_list_cursor,
)
from app.domain.tenancy import WorkspaceScope
from app.repositories.teams import TeamRepository
from app.services.issues import IssueService
from app.services.teams import TeamService

from tests.conftest import (
    TEST_SCOPE,
    ExplodingPool,
    FakeIssueRepository,
    FakePool,
    make_entity,
)


def build_service(rows=None):
    pool = FakePool()
    repository = FakeIssueRepository(rows)

    return (
        IssueService(
            pool=pool,
            repository=repository,
            teams=TeamService(pool=pool, repository=TeamRepository()),
        ),
        pool,
        repository,
    )


@pytest.mark.parametrize("first", [0, -1, 101, 1000])
async def test_invalid_first_fails_before_pool_acquire(first):
    pool = ExplodingPool()
    repository = FakeIssueRepository()
    service = IssueService(
        pool=pool,
        repository=repository,
        teams=TeamService(pool=pool, repository=TeamRepository()),
    )

    with pytest.raises(ValidationError) as exc_info:
        await service.list(scope=TEST_SCOPE, first=first, after=None)

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

    page = await service.list(scope=TEST_SCOPE, first=first, after=None)

    assert page.nodes == []
    assert pool.acquire_count == 1
    assert repository.list_calls[0]["limit"] == first + 1


async def test_first_page_without_extra_row():
    rows = [make_entity(2), make_entity(1)]
    service, _, repository = build_service(rows)

    page = await service.list(scope=TEST_SCOPE, first=2, after=None)

    assert len(page.nodes) == 2
    assert page.has_next_page is False

    assert repository.list_calls[0] == {
        "scope": TEST_SCOPE,
        "issue_filter": NO_FILTER,
        "order": IssueOrder(),
        "limit": 3,
        "after": None,
    }

    decoded = decode_issue_list_cursor(page.end_cursor)
    assert decoded.id == rows[1].id
    assert decoded.key == rows[1].created_at


async def test_extra_row_sets_has_next_page_and_is_trimmed():
    """The +1 row drives hasNextPage but must never be returned or encoded."""
    rows = [make_entity(3), make_entity(2), make_entity(1)]
    service, _, repository = build_service(rows)

    page = await service.list(scope=TEST_SCOPE, first=2, after=None)

    assert len(page.nodes) == 2
    assert page.has_next_page is True

    assert repository.list_calls[0]["limit"] == 3

    returned_ids = [node.id for node in page.nodes]
    assert returned_ids == [rows[0].id, rows[1].id]
    assert rows[2].id not in returned_ids

    # endCursor comes from the SECOND row, not the discarded third.
    decoded = decode_issue_list_cursor(page.end_cursor)
    assert decoded.id == rows[1].id
    assert decoded.key == rows[1].created_at
    assert decoded.id != rows[2].id


async def test_empty_result():
    service, _, _ = build_service(rows=[])

    page = await service.list(scope=TEST_SCOPE, first=10, after=None)

    assert page.nodes == []
    assert page.has_next_page is False
    assert page.end_cursor is None


async def test_after_cursor_is_decoded_and_passed_to_repository():
    entity = make_entity(5)
    cursor = encode_issue_list_cursor(IssueOrder(), entity.created_at, entity.id)

    service, _, repository = build_service(rows=[make_entity(1)])

    await service.list(scope=TEST_SCOPE, first=10, after=cursor)

    assert repository.list_calls[0] == {
        "scope": TEST_SCOPE,
        "issue_filter": NO_FILTER,
        "order": IssueOrder(),
        "limit": 11,
        "after": IssueListCursor(
            order=IssueOrder(),
            key=entity.created_at,
            id=entity.id,
        ),
    }


async def test_invalid_cursor_fails_before_repository_is_called():
    pool = ExplodingPool()
    repository = FakeIssueRepository()
    service = IssueService(
        pool=pool,
        repository=repository,
        teams=TeamService(pool=pool, repository=TeamRepository()),
    )

    with pytest.raises(ValidationError) as exc_info:
        await service.list(scope=TEST_SCOPE, first=10, after="not-a-valid-cursor")

    issues = exc_info.value.issues

    assert len(issues) == 1
    assert issues[0].field == "after"
    assert issues[0].code == "INVALID_CURSOR"
    assert issues[0].message == "Cursor is invalid"

    assert pool.acquire_count == 0
    assert repository.list_calls == []


async def test_each_call_carries_its_own_workspace_to_the_repository():
    """The service holds no workspace between calls and substitutes none.

    One service instance, two workspaces, in sequence -- which is what a
    worker looping over tenants does, and what a pooled or cached service
    would do under two concurrent requests. An implementation that stored
    the first scope, or fell back to a default, passes every other test in
    this file: they all use one workspace.
    """
    other = WorkspaceScope(workspace_id=UUID("00000000-0000-7000-8000-0000000000fc"))

    service, _, repository = build_service(rows=[])

    await service.list(scope=TEST_SCOPE, first=1, after=None)
    await service.list(scope=other, first=1, after=None)

    assert [call["scope"] for call in repository.list_calls] == [TEST_SCOPE, other]


async def test_first_and_cursor_errors_are_collected_together():
    pool = ExplodingPool()
    service = IssueService(
        pool=pool,
        repository=FakeIssueRepository(),
        teams=TeamService(pool=pool, repository=TeamRepository()),
    )

    with pytest.raises(ValidationError) as exc_info:
        await service.list(scope=TEST_SCOPE, first=0, after="bad")

    fields = [issue.field for issue in exc_info.value.issues]

    assert fields == ["first", "after"]
    assert pool.acquire_count == 0


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        (IssueOrderField.CREATED_AT, "created_at"),
        (IssueOrderField.UPDATED_AT, "updated_at"),
        (IssueOrderField.DUE_DATE, "due_date"),
    ],
)
async def test_the_end_cursor_carries_the_key_the_ordering_sorted_by(field, expected):
    """The cursor's key is read off the column that produced the order.

    Minting `created_at` while ordering by `updated_at` is the bug this
    guards: the walk resumes at a position in a sequence it never generated,
    so the second page starts somewhere arbitrary. Nothing fails; the rows
    are simply wrong.
    """
    rows = [make_entity(2), make_entity(1)]
    service, _, _ = build_service(rows)
    order = IssueOrder(field=field, direction=OrderDirection.DESC)

    page = await service.list(scope=TEST_SCOPE, first=2, after=None, order=order)

    decoded = decode_issue_list_cursor(page.end_cursor)

    assert decoded.order == order
    assert decoded.id == rows[1].id
    assert decoded.key == getattr(rows[1], expected)


async def test_an_untriaged_issue_mints_a_null_priority_key():
    """Priority 0 is "no priority", so it has no key in the urgency order.

    The repository sorts by `NULLIF(priority, 0)`, so the cursor has to carry
    the same null the column expression produces -- a cursor holding 0 would
    resume against a value the ordering never contains.
    """
    rows = [make_entity(1, priority=0)]
    service, _, _ = build_service(rows)

    page = await service.list(
        scope=TEST_SCOPE,
        first=1,
        after=None,
        order=IssueOrder(field=IssueOrderField.PRIORITY),
    )

    assert decode_issue_list_cursor(page.end_cursor).key is None


async def test_a_cursor_from_another_ordering_is_refused():
    """Resuming a keyset walk in a different sort returns the wrong rows.

    It does not error and it does not return nothing: the resume predicate
    compares a stored key against a different column, and what comes back is
    a plausible page made of rows the client has already seen or rows it
    never will. So the cursor carries its ordering and the mismatch is an
    input error, raised before a connection is taken.
    """
    entity = make_entity(5)
    cursor = encode_issue_list_cursor(IssueOrder(), entity.created_at, entity.id)

    pool = ExplodingPool()
    repository = FakeIssueRepository()
    service = IssueService(
        pool=pool,
        repository=repository,
        teams=TeamService(pool=pool, repository=TeamRepository()),
    )

    with pytest.raises(ValidationError) as exc_info:
        await service.list(
            scope=TEST_SCOPE,
            first=10,
            after=cursor,
            order=IssueOrder(field=IssueOrderField.PRIORITY),
        )

    issues = exc_info.value.issues

    assert [issue.code for issue in issues] == ["ORDER_MISMATCH"]
    assert issues[0].field == "after"

    assert pool.acquire_count == 0
    assert repository.list_calls == []


async def test_the_same_cursor_under_the_same_ordering_is_accepted():
    """The mismatch check must not refuse a walk that never changed sort."""
    entity = make_entity(5)
    order = IssueOrder(field=IssueOrderField.DUE_DATE, direction=OrderDirection.ASC)
    cursor = encode_issue_list_cursor(order, entity.due_date, entity.id)

    service, _, repository = build_service(rows=[])

    await service.list(scope=TEST_SCOPE, first=10, after=cursor, order=order)

    assert repository.list_calls[0]["after"].id == entity.id
    assert repository.list_calls[0]["order"] == order


@pytest.mark.parametrize("priority", [-1, 5, 100])
async def test_an_impossible_priority_filter_is_refused_before_the_pool(priority):
    """No row can hold a priority outside 0..4, so this is not a lookup.

    `issues_priority_range` guarantees it, which makes the request one no
    state of the database could satisfy rather than one that happens to
    match nothing -- and the codes are the ones create and update already
    publish for the same field.
    """
    pool = ExplodingPool()
    repository = FakeIssueRepository()
    service = IssueService(
        pool=pool,
        repository=repository,
        teams=TeamService(pool=pool, repository=TeamRepository()),
    )

    with pytest.raises(ValidationError) as exc_info:
        await service.list(
            scope=TEST_SCOPE,
            first=10,
            after=None,
            issue_filter=IssueFilter(priority=priority),
        )

    assert [issue.field for issue in exc_info.value.issues] == ["priority"]
    assert pool.acquire_count == 0
    assert repository.list_calls == []


async def test_an_id_filter_naming_nothing_is_an_empty_page_and_not_an_error():
    """A project from another workspace must answer as one that never existed.

    Validating it here would mean the service could tell a caller that
    someone else's project is real, which is the distinction CLAUDE.md
    requires stay invisible.
    """
    service, _, repository = build_service(rows=[])

    page = await service.list(
        scope=TEST_SCOPE,
        first=10,
        after=None,
        issue_filter=IssueFilter(
            project_id=UUID("00000000-0000-7000-8000-00000000dead")
        ),
    )

    assert page.nodes == []
    assert len(repository.list_calls) == 1


async def test_count_forwards_the_filter_and_validates_it():
    service, pool, repository = build_service(rows=[])
    repository.total = 42

    issue_filter = IssueFilter(assignee_id=None)

    assert await service.count(scope=TEST_SCOPE, issue_filter=issue_filter) == 42
    assert repository.count_calls == [
        {"scope": TEST_SCOPE, "issue_filter": issue_filter}
    ]
    assert pool.acquire_count == 1


async def test_count_refuses_the_same_impossible_filter_the_list_does():
    pool = ExplodingPool()
    repository = FakeIssueRepository()
    service = IssueService(
        pool=pool,
        repository=repository,
        teams=TeamService(pool=pool, repository=TeamRepository()),
    )

    with pytest.raises(ValidationError):
        await service.count(scope=TEST_SCOPE, issue_filter=IssueFilter(priority=9))

    assert pool.acquire_count == 0
    assert repository.count_calls == []
