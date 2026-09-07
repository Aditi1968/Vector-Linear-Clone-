"""Repository SQL tests against a fake asyncpg connection.

Every test here reads the statement text, because the properties that matter
about this one are properties of the SQL: which predicate leads, whether a
client value ever reaches the string, and whether the ORDER BY still ends in a
unique column. A test that only checked the rows coming back would pass
against a statement that had lost its tenant predicate.
"""

from datetime import date
from uuid import UUID, uuid4

import pytest

from app.domain.issues import (
    NO_FILTER,
    IssueEntity,
    IssueFilter,
    IssueOrder,
    IssueOrderField,
    OrderDirection,
)
from app.domain.pagination import IssueListCursor
from app.domain.teams import WorkflowStateCategory
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


async def list_query(**kwargs) -> tuple[str, tuple]:
    """Run `list` against a fake connection and hand back the statement it made."""
    connection = FakeConnection(rows=[])

    await IssueRepository().list(
        connection,
        scope=kwargs.pop("scope", TEST_SCOPE),
        issue_filter=kwargs.pop("issue_filter", NO_FILTER),
        order=kwargs.pop("order", IssueOrder()),
        limit=kwargs.pop("limit", 51),
        after=kwargs.pop("after", None),
    )

    assert not kwargs

    return normalize(connection.queries[0]["query"]), connection.queries[0]["args"]


async def test_no_cursor_page_is_still_scoped_to_one_workspace():
    """There is no unscoped listing query, cursor or not.

    The first page is the easy one to get wrong: with no cursor and no filter
    there is nothing else in the WHERE clause, so an implementation that
    forgot the tenant here produces a syntactically clean statement that reads
    every issue in the product.
    """
    query, args = await list_query()

    assert "WHERE issues.workspace_id = $1" in query
    assert "AND issues.archived_at IS NULL" in query
    assert "ORDER BY issues.created_at DESC, issues.id DESC" in query
    assert "LIMIT $2" in query
    assert "OFFSET" not in query

    assert args == (TEST_WORKSPACE_ID, 51)
    assert str(TEST_WORKSPACE_ID) not in query


async def test_cursor_path_uses_row_value_comparison_inside_the_workspace():
    """The tenant is an equality; only (created_at, id) is the keyset.

    Widening the row-value comparison to include `workspace_id` would put
    workspaces into the ordering, and a walk that reached the end of one
    tenant would carry on into the next.
    """
    entity = make_entity(3)

    query, args = await list_query(
        limit=11,
        after=IssueListCursor(
            order=IssueOrder(),
            key=entity.created_at,
            id=entity.id,
        ),
    )

    assert "WHERE issues.workspace_id = $1" in query
    assert "AND (issues.created_at, issues.id) < ($2, $3)" in query
    assert "ORDER BY issues.created_at DESC, issues.id DESC" in query
    assert "LIMIT $4" in query
    assert "OFFSET" not in query

    assert args == (TEST_WORKSPACE_ID, entity.created_at, entity.id, 11)
    assert str(entity.id) not in query
    assert str(TEST_WORKSPACE_ID) not in query


async def test_records_are_converted_to_entities():
    entities = [make_entity(2), make_entity(1)]
    connection = FakeConnection(rows=[as_record(e) for e in entities])

    result = await IssueRepository().list(
        connection,
        scope=TEST_SCOPE,
        issue_filter=NO_FILTER,
        order=IssueOrder(),
        limit=3,
        after=None,
    )

    assert result == entities
    assert all(isinstance(item, IssueEntity) for item in result)


async def test_limit_receives_first_plus_one_from_service():
    """The repository is handed first+1 verbatim; it does not adjust it."""
    _, args = await list_query(limit=50 + 1)

    assert args == (TEST_WORKSPACE_ID, 51)


async def test_a_team_filter_narrows_within_the_workspace_and_never_widens_it():
    """The team is ANDed onto the tenant predicate, never substituted for it.

    A team id arrives from a client, so the failure to refuse is the one that
    matters: a statement selecting on `team_id` alone would hand back another
    workspace's issues to anyone who could guess one of its team ids. The
    workspace equality stays first and the team is an additional restriction,
    so a foreign team intersects with nothing.
    """
    query, args = await list_query(issue_filter=IssueFilter(team_id=TEST_TEAM_ID))

    assert "WHERE issues.workspace_id = $1" in query
    assert "AND issues.team_id = $2" in query

    assert args == (TEST_WORKSPACE_ID, TEST_TEAM_ID, 51)
    assert str(TEST_TEAM_ID) not in query


# Every id-shaped filter, and the predicate it is required to become. The
# point of the table is exhaustiveness: a filter added without a workspace
# predicate beside it is exactly the bug this file exists to catch, and a
# per-filter test written by hand is the one somebody forgets to write.
ID_FILTERS = [
    ("team_id", "issues.team_id = $2"),
    ("assignee_id", "issues.assignee_id = $2"),
    ("workflow_state_id", "issues.workflow_state_id = $2"),
    ("project_id", "issues.project_id = $2"),
    ("cycle_id", "issues.cycle_id = $2"),
]


@pytest.mark.parametrize(("field", "predicate"), ID_FILTERS)
async def test_every_id_filter_is_anded_onto_the_workspace(field, predicate):
    value = uuid4()

    query, args = await list_query(issue_filter=IssueFilter(**{field: value}))

    # The tenant predicate leads and the filter is an extra restriction, so
    # an id from another workspace can only ever remove rows.
    assert query.index("WHERE issues.workspace_id = $1") < query.index(predicate)
    assert args == (TEST_WORKSPACE_ID, value, 51)
    assert str(value) not in query


@pytest.mark.parametrize("field", ["assignee_id", "project_id", "cycle_id"])
async def test_a_null_filter_becomes_is_null_and_binds_nothing(field):
    """`= NULL` is never true, so "unassigned" cannot be a bound parameter.

    A filter that bound None would return an empty page rather than the rows
    holding nothing -- silently, with a perfectly valid statement.
    """
    query, args = await list_query(issue_filter=IssueFilter(**{field: None}))

    assert f"AND issues.{field} IS NULL" in query
    assert args == (TEST_WORKSPACE_ID, 51)


async def test_the_label_filter_scopes_its_own_subquery_to_the_workspace():
    """A label id from another tenant must match nothing, not join freely.

    `issue_labels` carries its own `workspace_id`, so the EXISTS has to name
    $1 rather than joining on the issue alone -- otherwise a guessed label id
    from another workspace would select this workspace's issues that happen to
    share it, which is a cross-tenant read even though every row returned
    belongs to the caller.
    """
    label = uuid4()

    query, args = await list_query(issue_filter=IssueFilter(label_id=label))

    assert "EXISTS (" in query
    assert "FROM issue_labels" in query
    assert "WHERE issue_labels.workspace_id = $1" in query
    assert "AND issue_labels.issue_id = issues.id" in query
    assert "AND issue_labels.label_id = $2" in query

    assert args == (TEST_WORKSPACE_ID, label, 51)


async def test_the_state_category_filter_scopes_its_own_subquery_too():
    """The one filter that leaves the table still cannot leave the tenant."""
    query, args = await list_query(
        issue_filter=IssueFilter(state_category=WorkflowStateCategory.COMPLETED)
    )

    assert "SELECT workflow_states.id" in query
    assert "WHERE workflow_states.workspace_id = $1" in query
    assert "AND workflow_states.type = $2" in query

    assert args == (TEST_WORKSPACE_ID, "completed", 51)


async def test_a_priority_filter_is_bound_and_not_interpolated():
    query, args = await list_query(issue_filter=IssueFilter(priority=2))

    assert "AND issues.priority = $2" in query
    assert args == (TEST_WORKSPACE_ID, 2, 51)


async def test_filters_compose_and_each_gets_its_own_parameter():
    """Eight filters at once, each a separate $n, none of them in the text."""
    team, assignee, state, label, project, cycle = (uuid4() for _ in range(6))

    query, args = await list_query(
        issue_filter=IssueFilter(
            team_id=team,
            assignee_id=assignee,
            workflow_state_id=state,
            state_category=WorkflowStateCategory.STARTED,
            label_id=label,
            priority=1,
            project_id=project,
            cycle_id=cycle,
        )
    )

    assert args == (
        TEST_WORKSPACE_ID,
        team,
        assignee,
        state,
        "started",
        label,
        1,
        project,
        cycle,
        51,
    )

    for value in (team, assignee, state, label, project, cycle):
        assert str(value) not in query


ORDERINGS = [
    (IssueOrderField.CREATED_AT, "issues.created_at"),
    (IssueOrderField.UPDATED_AT, "issues.updated_at"),
    (IssueOrderField.DUE_DATE, "issues.due_date"),
    (IssueOrderField.PRIORITY, "NULLIF(issues.priority, 0)"),
]


@pytest.mark.parametrize(("field", "key_sql"), ORDERINGS)
@pytest.mark.parametrize("direction", list(OrderDirection))
async def test_every_ordering_ends_in_the_id(field, key_sql, direction):
    """No ordering here is allowed to be non-total.

    `created_at` ties under any bulk import and `priority` has five distinct
    values across the whole table, so the sort key alone never determines a
    row's position. `id` is unique, so appending it makes every ordering
    total -- and a keyset walk over a non-total order silently skips and
    repeats rows rather than failing.
    """
    query, _ = await list_query(order=IssueOrder(field=field, direction=direction))

    word = "DESC" if direction is OrderDirection.DESC else "ASC"

    assert f"ORDER BY {key_sql} {word}, issues.id {word}" in query


@pytest.mark.parametrize(("field", "key_sql"), ORDERINGS)
@pytest.mark.parametrize("direction", list(OrderDirection))
async def test_the_keyset_resumes_on_the_same_key_it_ordered_by(
    field, key_sql, direction
):
    """The resume predicate compares the column the ORDER BY named.

    Ordering on one key and resuming on another is the classic keyset bug:
    the page walk still returns rows, in an order that looks right, and
    quietly drops the ones between the two keys.
    """
    query, _ = await list_query(
        order=IssueOrder(field=field, direction=direction),
        after=IssueListCursor(
            order=IssueOrder(field=field, direction=direction),
            key=7 if field is IssueOrderField.PRIORITY else make_entity(1).created_at,
            id=uuid4(),
        ),
    )

    operator = "<" if direction is OrderDirection.DESC else ">"

    assert f"({key_sql}, issues.id) {operator} ($2, $3)" in query


async def test_ascending_over_a_nullable_key_keeps_the_null_rows_ahead():
    """ASC is NULLS LAST, so a page resumed mid-way still owes them.

    Without the `IS NULL OR` the walk would drop every undated issue: they
    sort after all the dated ones, but `(NULL, id) > (k, i)` is NULL rather
    than true, so the comparison alone excludes exactly the rows still to
    come.
    """
    order = IssueOrder(field=IssueOrderField.DUE_DATE, direction=OrderDirection.ASC)

    query, _ = await list_query(
        order=order,
        after=IssueListCursor(order=order, key=date(2026, 3, 1), id=uuid4()),
    )

    assert (
        "(issues.due_date IS NULL OR (issues.due_date, issues.id) > ($2, $3))"
    ) in query


async def test_resuming_from_a_null_key_ascending_stays_among_the_nulls():
    """Once the walk is in the NULLS LAST tail there is nothing else left."""
    order = IssueOrder(field=IssueOrderField.DUE_DATE, direction=OrderDirection.ASC)
    after_id = uuid4()

    query, args = await list_query(
        order=order,
        after=IssueListCursor(order=order, key=None, id=after_id),
    )

    assert "(issues.due_date IS NULL AND issues.id > $2)" in query
    assert args == (TEST_WORKSPACE_ID, after_id, 51)


async def test_resuming_from_a_null_key_descending_still_owes_the_non_nulls():
    """DESC is NULLS FIRST, so the untriaged come first and the rest follow.

    A predicate of `IS NULL AND id < $n` here -- the mirror of the ascending
    case -- would return the remaining untriaged issues and then stop,
    silently truncating the list at the point where the real priorities
    begin.
    """
    order = IssueOrder(field=IssueOrderField.PRIORITY, direction=OrderDirection.DESC)
    after_id = uuid4()

    query, args = await list_query(
        order=order,
        after=IssueListCursor(order=order, key=None, id=after_id),
    )

    assert (
        "(NULLIF(issues.priority, 0) IS NOT NULL "
        "OR (NULLIF(issues.priority, 0) IS NULL AND issues.id < $2))"
    ) in query
    assert args == (TEST_WORKSPACE_ID, after_id, 51)


async def test_count_shares_the_filter_and_drops_the_ordering():
    """The number and the page must answer the same question.

    Same tenant predicate, same live predicate, same filters -- built by the
    same code, so the two cannot disagree about what a filter means. No ORDER
    BY and no LIMIT, because an aggregate over a set has no position in it.
    """
    project = uuid4()
    connection = FakeConnection(value=12)

    total = await IssueRepository().count(
        connection,
        scope=TEST_SCOPE,
        issue_filter=IssueFilter(project_id=project, priority=0),
    )

    query = normalize(connection.queries[0]["query"])

    assert total == 12
    assert "SELECT count(*)" in query
    assert "WHERE issues.workspace_id = $1" in query
    assert "AND issues.archived_at IS NULL" in query
    # The parameter numbers follow `_add_filters`' own order, not the order
    # the caller happened to name the fields in.
    assert "AND issues.priority = $2" in query
    assert "AND issues.project_id = $3" in query
    assert "ORDER BY" not in query
    assert "LIMIT" not in query

    assert connection.queries[0]["args"] == (TEST_WORKSPACE_ID, 0, project)


async def test_count_of_the_whole_workspace_is_still_scoped():
    connection = FakeConnection(value=0)

    await IssueRepository().count(
        connection,
        scope=TEST_SCOPE,
        issue_filter=NO_FILTER,
    )

    query = normalize(connection.queries[0]["query"])

    assert "WHERE issues.workspace_id = $1" in query
    assert connection.queries[0]["args"] == (TEST_WORKSPACE_ID,)


async def test_a_second_workspace_binds_its_own_id():
    """The repository holds no tenant between calls and substitutes none."""
    other = UUID("00000000-0000-7000-8000-0000000000ff")

    _, first_args = await list_query()
    _, second_args = await list_query(scope=TEST_SCOPE.__class__(workspace_id=other))

    assert first_args[0] == TEST_WORKSPACE_ID
    assert second_args[0] == other
