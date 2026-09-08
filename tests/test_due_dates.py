"""Due-date filtering, and the estimate rule that needs the team.

Two features that both turn on a value the request does not carry -- today, and
the team's scale -- and both are checked here by reading the statements the
repository sends. A tenant predicate missing from a WHERE clause is invisible to
every test that only checks what came back, because a fake connection agrees
with whatever SQL it is handed; so does a database holding one tenant.

The claims that matter:

  * EVERY DUE PREDICATE IS A BOUND ON `due_date`. That is what lets migration
    015's `issues_workspace_live_due_date_id_idx` serve them as ranges rather
    than as filters applied to rows that had to be fetched first, and it is the
    reason migration 029 adds no index. A predicate written as
    `date_trunc(...)` or `age(...)` would be correct and would silently stop
    using it.
  * TODAY COMES FROM THE SERVER. `CURRENT_DATE` in the statement, never a bound
    parameter -- which is the whole reason the window is a vocabulary rather
    than two dates a client sends.
  * NOTHING WIDENS. Every predicate is ANDed onto the tenant equality.
  * AN ESTIMATE IS CHECKED AGAINST THE TEAM THAT OWNS THE ISSUE, and the read
    that answers that question is scoped like every other read on the class.

No `db` mark: nothing here reaches PostgreSQL. tests/test_migration_029_db.py
runs the same predicates against a real server.
"""

from datetime import date
from uuid import UUID

import pytest

from app.domain.estimates import EstimateScale
from app.domain.issues import (
    DEFAULT_ORDER,
    THIS_WEEK_DAYS,
    DueWindow,
    IssueFilter,
    IssueOrder,
    IssueOrderField,
    OrderDirection,
)
from app.domain.pagination import IssueListCursor
from app.domain.tenancy import WorkspaceScope
from app.repositories.issues import IssueRepository

from tests.conftest import FakeConnection, normalize


WORKSPACE_ID = UUID("00000000-0000-7000-8000-000000000001")
ISSUE_ID = UUID("00000000-0000-7000-8000-000000000009")
SCOPE = WorkspaceScope(workspace_id=WORKSPACE_ID)


async def sent_for(issue_filter: IssueFilter) -> dict:
    """The statement `list` sends for one filter, and its parameters."""
    connection = FakeConnection(rows=[])

    await IssueRepository().list(
        connection,
        scope=SCOPE,
        issue_filter=issue_filter,
        order=DEFAULT_ORDER,
        limit=51,
        after=None,
    )

    return connection.queries[0]


# --- the four relative windows ---------------------------------------


@pytest.mark.parametrize(
    ("window", "predicate"),
    [
        # Strictly before today, and NULL is excluded for free -- `NULL <
        # CURRENT_DATE` is NULL rather than true, so an issue with no due date
        # has missed nothing without a second predicate saying so.
        (DueWindow.OVERDUE, "issues.due_date < CURRENT_DATE"),
        (DueWindow.TODAY, "issues.due_date = CURRENT_DATE"),
        # Half-open, so the arithmetic reads as "seven days" once rather than
        # as "six" with a comment explaining the seventh.
        (
            DueWindow.THIS_WEEK,
            f"issues.due_date >= CURRENT_DATE AND issues.due_date < "
            f"CURRENT_DATE + {THIS_WEEK_DAYS}",
        ),
        # A btree indexes NULLs, so this is a range at the far end of the
        # due-date index rather than a scan -- the same property migration 015
        # relies on for `assigneeId: null`.
        (DueWindow.NONE, "issues.due_date IS NULL"),
    ],
)
async def test_each_window_is_a_bound_on_the_due_date_column(window, predicate):
    sent = await sent_for(IssueFilter(due_window=window))

    assert normalize(predicate) in normalize(sent["query"])


@pytest.mark.parametrize("window", list(DueWindow))
async def test_no_window_binds_today_as_a_parameter(window):
    """`CURRENT_DATE` and not `$n`, which is the point of the window existing.

    A client sending its own idea of today would give two colleagues in two
    timezones two different lists from one screen, and would make a saved
    "Overdue" view resolve against the day it was saved. The only parameters
    this statement carries are the workspace and the limit.
    """
    sent = await sent_for(IssueFilter(due_window=window))

    assert sent["args"] == (WORKSPACE_ID, 51)
    assert "CURRENT_DATE" in sent["query"] or window is DueWindow.NONE


@pytest.mark.parametrize("window", list(DueWindow))
async def test_every_window_narrows_rather_than_replacing_the_tenant_predicate(window):
    sent = await sent_for(IssueFilter(due_window=window))
    statement = normalize(sent["query"])

    assert "issues.workspace_id = $1" in statement
    assert "issues.archived_at IS NULL" in statement


# --- the absolute range ----------------------------------------------


async def test_a_range_binds_both_ends_and_includes_them():
    """Inclusive at both ends, which is what dragging two days on a calendar
    means; an exclusive end would make the last day of a sprint not part of
    it."""
    sent = await sent_for(
        IssueFilter(due_after=date(2026, 3, 1), due_before=date(2026, 3, 31))
    )
    statement = normalize(sent["query"])

    assert "issues.due_date >= $2" in statement
    assert "issues.due_date <= $3" in statement
    assert sent["args"] == (WORKSPACE_ID, date(2026, 3, 1), date(2026, 3, 31), 51)


async def test_one_end_of_a_range_is_a_filter_on_its_own():
    sent = await sent_for(IssueFilter(due_before=date(2026, 3, 31)))
    statement = normalize(sent["query"])

    assert "issues.due_date <= $2" in statement
    assert "issues.due_date >=" not in statement


async def test_a_window_and_a_range_are_anded_rather_than_one_replacing_the_other():
    """Both narrow, so combining them is a coherent request that needs no
    special case -- and a range that excludes itself selects nothing rather
    than erroring, exactly as an id from another workspace does."""
    sent = await sent_for(
        IssueFilter(due_window=DueWindow.OVERDUE, due_after=date(2026, 1, 1))
    )
    statement = normalize(sent["query"])

    assert "issues.due_date < CURRENT_DATE" in statement
    assert "issues.due_date >= $2" in statement


async def test_a_filter_naming_no_due_field_emits_no_due_predicate():
    """The wide list is unchanged by this feature existing, which is what keeps
    an existing document's pages the pages it had.

    Asserted over the outer WHERE clause rather than the whole statement:
    `due_date` is one of the columns `ISSUE_COLUMNS` always selects, so a
    substring test over the text would fail for a statement that selected the
    column it has selected since 006.

    Split on the tenant predicate rather than on the word WHERE, because
    `ISSUE_COLUMNS` carries one of its own -- the correlated subquery that
    resolves `team_key`.
    """
    sent = await sent_for(IssueFilter())
    where = normalize(sent["query"]).split("WHERE issues.workspace_id = $1", 1)[1]

    assert "due_date" not in where


# --- the count uses the same predicates ------------------------------


async def test_the_count_narrows_by_the_same_due_predicate_as_the_page():
    """Both go through `_add_filters`, so the number a column header states
    cannot disagree with the rows under it."""
    connection = FakeConnection(value=3)

    await IssueRepository().count(
        connection,
        scope=SCOPE,
        issue_filter=IssueFilter(due_window=DueWindow.OVERDUE),
    )

    statement = normalize(connection.queries[0]["query"])

    assert "issues.due_date < CURRENT_DATE" in statement
    assert "issues.workspace_id = $1" in statement


# --- the keyset still walks the due-date ordering --------------------


async def test_a_due_filter_leaves_the_keyset_comparison_alone():
    """A due predicate is an equality-shaped filter and never part of the
    ordering key.

    Folding one into the row-value comparison is how a page walk leaves the set
    it was asked for -- the same hazard the tenant predicate is kept out of the
    keyset for.
    """
    connection = FakeConnection(rows=[])
    order = IssueOrder(field=IssueOrderField.DUE_DATE, direction=OrderDirection.ASC)

    await IssueRepository().list(
        connection,
        scope=SCOPE,
        issue_filter=IssueFilter(due_window=DueWindow.OVERDUE),
        order=order,
        limit=51,
        after=IssueListCursor(order=order, key=date(2026, 3, 1), id=ISSUE_ID),
    )

    statement = normalize(connection.queries[0]["query"])

    assert "issues.due_date < CURRENT_DATE" in statement
    assert "(issues.due_date, issues.id) > ($2, $3)" in statement
    assert "ORDER BY issues.due_date ASC, issues.id ASC" in statement


# --- the estimate scale lookup ---------------------------------------


async def test_the_scale_is_read_through_the_issues_own_team():
    """The join is on the ISSUE's stored `(workspace_id, team_id)`.

    So the scale that governs a write is the one belonging to the team the
    database has, never one derived from anything a caller sent -- which is the
    same discipline every other statement on this class keeps.
    """
    connection = FakeConnection(value="tshirt")

    scale = await IssueRepository().find_estimate_scale(
        connection,
        scope=SCOPE,
        issue_id=ISSUE_ID,
    )

    sent = connection.queries[0]
    statement = normalize(sent["query"])

    assert scale is EstimateScale.TSHIRT
    assert "teams.workspace_id = issues.workspace_id" in statement
    assert "teams.id = issues.team_id" in statement
    assert "issues.workspace_id = $1" in statement
    assert "issues.archived_at IS NULL" in statement
    assert sent["args"] == (WORKSPACE_ID, ISSUE_ID)


async def test_an_issue_that_is_not_here_has_no_scale_to_check():
    """Nonexistent, another tenant's and archived are one answer, and the
    caller does not turn it into an error -- the update that follows matches
    nothing and answers None, which is what this method's siblings do for all
    three."""
    connection = FakeConnection(value=None)

    assert (
        await IssueRepository().find_estimate_scale(
            connection,
            scope=SCOPE,
            issue_id=ISSUE_ID,
        )
        is None
    )
