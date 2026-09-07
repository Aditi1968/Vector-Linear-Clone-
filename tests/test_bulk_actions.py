"""Bulk actions, without a database.

Two things are worth asserting with no server in the way, and they are the two
that a database test would prove slowly or not at all.

The first is that nothing reaches a connection unless the request could
possibly be satisfied. A batch of ten thousand ids, a patch that changes
nothing, a milestone with no project: each is a request no state of the
database could answer, so refusing it before `pool.acquire()` is both the
cheaper answer and the one that cannot be raced. `ExplodingPool` turns any
acquire into a failed test, which is how "validated first" is asserted rather
than assumed.

The second is the count comparison that makes a batch all-or-nothing. It is
one line in `BulkService._lock` and it is the whole authorisation of the
feature: an id from another workspace does not come back from the scoped lock,
so the counts disagree and the batch is refused. Here that is exercised over a
fake connection that simply returns fewer rows than were asked about -- which
is exactly what the real statement does for a foreign id, with none of the
setup. `tests/test_bulk_actions_db.py` proves the other half, that the refusal
actually rolls back writes already issued.
"""

from uuid import UUID

import pytest

from app.domain.bulk import BULK_MAX, BulkIssuePatch
from app.domain.errors import ValidationError
from app.domain.issues import UNSET
from app.repositories.bulk import BulkRepository
from app.repositories.issue_labels import IssueLabelRepository
from app.services.bulk import BulkService

from tests.conftest import (
    TEST_SCOPE,
    ExplodingPool,
    FakeConnection,
    FakePool,
    normalize,
)


ISSUE_IDS = [UUID(int=index) for index in range(1, 4)]
LABEL_ID = UUID("00000000-0000-7000-8000-0000000000d1")
STATE_ID = UUID("00000000-0000-7000-8000-0000000000d2")
PROJECT_ID = UUID("00000000-0000-7000-8000-0000000000d3")
MILESTONE_ID = UUID("00000000-0000-7000-8000-0000000000d4")


def bulk_service(pool) -> BulkService:
    return BulkService(
        pool=pool,
        repository=BulkRepository(),
        issue_labels=IssueLabelRepository(),
    )


def snapshot_row(issue_id: UUID) -> dict:
    """asyncpg.Record supports __getitem__, which a dict models well enough.

    Keyed by the column names `BulkRepository.lock_snapshots` selects, so a
    column added to `IssueSnapshot` and forgotten in that statement fails here
    rather than at runtime.
    """
    return {
        "id": issue_id,
        "title": "Something",
        "priority": 1,
        "workflow_state_id": STATE_ID,
        "assignee_id": None,
        "project_id": None,
        "cycle_id": None,
    }


class SequencedConnection(FakeConnection):
    """A FakeConnection that answers each `fetch` with the next canned result.

    `FakeConnection` replays ONE result for every query, which is enough for a
    repository test and not enough here: a bulk update issues a lock, then an
    update, and the two return different row shapes. Replaying the snapshot
    rows for both makes `_issue_entity` raise a KeyError that reports the fake
    rather than the behaviour.

    Beyond the canned list every query answers with nothing, which is what the
    history writes want -- they are inserts and their results are not read.
    """

    def __init__(self, results):
        super().__init__()

        self._results = list(results)

    async def fetch(self, query, *args):
        self.queries.append({"query": query, "args": args})

        if self._results:
            return self._results.pop(0)

        return []


def issue_row(issue_id: UUID) -> dict:
    """Keyed by the column names `ISSUE_COLUMNS` produces, not by the entity's
    attribute names -- the two differ at `team_key`, which is a subquery alias
    rather than a column of `issues`."""
    return {
        "id": issue_id,
        "team_id": UUID(int=99),
        "team_key": "ENG",
        "number": 1,
        "title": "Something",
        "description": None,
        "priority": 2,
        "workflow_state_id": STATE_ID,
        "assignee_id": None,
        "creator_id": None,
        "estimate": None,
        "due_date": None,
        "cycle_id": None,
        "project_id": None,
        "milestone_id": None,
        "completed_at": None,
        "archived_at": None,
        "created_at": None,
        "updated_at": None,
    }


def wired(locked: list[UUID], updated: list[UUID]) -> tuple[FakePool, FakeConnection]:
    """A pool over a connection that answers the lock, then the update."""
    connection = SequencedConnection(
        [
            [snapshot_row(issue_id) for issue_id in locked],
            [issue_row(issue_id) for issue_id in updated],
        ]
    )

    return FakePool(connection), connection


def codes(exc: ValidationError) -> list[str]:
    return [issue.code for issue in exc.issues]


def fields(exc: ValidationError) -> list[str]:
    return [issue.field for issue in exc.issues]


# --- the batch is bounded, and refused rather than truncated -----------


@pytest.mark.parametrize(
    ("count", "label"),
    [(0, "empty"), (BULK_MAX + 1, "one-over")],
)
async def test_an_out_of_range_batch_never_reaches_the_pool(
    exploding_pool: ExplodingPool, count, label
):
    """An unbounded id list from a browser is a denial-of-service vector.

    Every id in the batch is locked FOR UPDATE for the life of the
    transaction, so the list length is also the number of issues nobody else
    can edit while it runs. The bound is checked before a connection is taken,
    so the request costs nothing at all.
    """
    service = bulk_service(exploding_pool)

    with pytest.raises(ValidationError) as raised:
        await service.update_many(
            scope=TEST_SCOPE,
            issue_ids=[UUID(int=index) for index in range(count)],
            patch=BulkIssuePatch(priority=2),
        )

    assert codes(raised.value) == ["OUT_OF_RANGE"]
    assert exploding_pool.acquire_count == 0


async def test_the_bound_is_checked_before_duplicates_are_removed(
    exploding_pool: ExplodingPool,
):
    """A client that sent ten thousand ids has sent ten thousand ids.

    Deduplicating first would let one repeated id smuggle an arbitrarily long
    list past the bound -- the parse and the array parameter still cost what
    they cost, whatever the ids resolve to.
    """
    service = bulk_service(exploding_pool)

    with pytest.raises(ValidationError) as raised:
        await service.update_many(
            scope=TEST_SCOPE,
            issue_ids=[ISSUE_IDS[0]] * (BULK_MAX + 1),
            patch=BulkIssuePatch(priority=2),
        )

    assert codes(raised.value) == ["OUT_OF_RANGE"]
    assert exploding_pool.acquire_count == 0


async def test_a_batch_at_the_ceiling_is_accepted(exploding_pool: ExplodingPool):
    """The boundary from the other side, so the bound is not merely a ban.

    The acquire is what proves validation passed: `ExplodingPool` raises on it,
    and an AssertionError here means the request was accepted and went looking
    for a connection.
    """
    service = bulk_service(exploding_pool)

    with pytest.raises(AssertionError):
        await service.update_many(
            scope=TEST_SCOPE,
            issue_ids=[UUID(int=index) for index in range(BULK_MAX)],
            patch=BulkIssuePatch(priority=2),
        )


# --- a request that would change nothing --------------------------------


async def test_a_batch_that_changes_nothing_never_reaches_the_pool(
    exploding_pool: ExplodingPool,
):
    """Every write here stamps `updated_at`, so an empty request would record
    an edit that changed nothing across the whole selection."""
    service = bulk_service(exploding_pool)

    with pytest.raises(ValidationError) as raised:
        await service.update_many(scope=TEST_SCOPE, issue_ids=ISSUE_IDS)

    assert codes(raised.value) == ["EMPTY"]
    assert exploding_pool.acquire_count == 0


async def test_a_label_only_batch_is_not_empty(exploding_pool: ExplodingPool):
    """Attaching a label changes no column of `issues`, and is still a change.

    The empty check is therefore about the whole request rather than about the
    patch, which is the one thing `BulkIssuePatch.is_empty` deliberately does
    not decide on its own.
    """
    service = bulk_service(exploding_pool)

    with pytest.raises(AssertionError):
        await service.update_many(
            scope=TEST_SCOPE,
            issue_ids=ISSUE_IDS,
            add_label_ids=[LABEL_ID],
        )


# --- the field rules the arguments alone decide -------------------------


async def test_a_project_and_a_milestone_must_move_together(
    exploding_pool: ExplodingPool,
):
    """`issues_milestone_fk` ties a milestone to the row's OWN project.

    Moving a selection into a project while leaving their milestones alone
    would leave each issue pointing at a milestone of the project it just left
    -- a row the server refuses, reported as a constraint violation the client
    cannot read. Refusing it here says what to send instead.
    """
    service = bulk_service(exploding_pool)

    with pytest.raises(ValidationError) as raised:
        await service.update_many(
            scope=TEST_SCOPE,
            issue_ids=ISSUE_IDS,
            patch=BulkIssuePatch(project_id=PROJECT_ID),
        )

    assert codes(raised.value) == ["PROJECT_REQUIRED"]
    assert exploding_pool.acquire_count == 0


async def test_clearing_both_project_and_milestone_is_allowed(
    exploding_pool: ExplodingPool,
):
    """The three-state distinction, from the side that is easy to get wrong.

    UNSET is "not moving the project", None is "take these out of their
    project". A check written as `milestone_id is not UNSET and project_id is
    None` would refuse this -- the ordinary "remove these from their project"
    -- while looking correct.
    """
    service = bulk_service(exploding_pool)

    with pytest.raises(AssertionError):
        await service.update_many(
            scope=TEST_SCOPE,
            issue_ids=ISSUE_IDS,
            patch=BulkIssuePatch(project_id=None, milestone_id=None),
        )


async def test_a_milestone_without_a_project_is_refused(
    exploding_pool: ExplodingPool,
):
    service = bulk_service(exploding_pool)

    with pytest.raises(ValidationError) as raised:
        await service.update_many(
            scope=TEST_SCOPE,
            issue_ids=ISSUE_IDS,
            patch=BulkIssuePatch(project_id=None, milestone_id=MILESTONE_ID),
        )

    assert codes(raised.value) == ["PROJECT_REQUIRED"]


@pytest.mark.parametrize(
    ("patch", "field"),
    [
        (BulkIssuePatch(workflow_state_id=None), "workflowStateId"),
        (BulkIssuePatch(priority=None), "priority"),
    ],
)
async def test_the_not_null_columns_cannot_be_cleared(
    exploding_pool: ExplodingPool, patch, field
):
    """GraphQL cannot express "optional but never null".

    An input field is required exactly when it is non-null with no default, so
    a field that may be omitted from a patch is necessarily one that may arrive
    as null. Refusing it here turns what would otherwise be a NOT NULL
    violation from the driver into the field error it actually is.
    """
    service = bulk_service(exploding_pool)

    with pytest.raises(ValidationError) as raised:
        await service.update_many(scope=TEST_SCOPE, issue_ids=ISSUE_IDS, patch=patch)

    assert fields(raised.value) == [field]
    assert codes(raised.value) == ["NOT_NULLABLE"]


@pytest.mark.parametrize("priority", [-1, 5])
async def test_a_priority_outside_the_range_never_reaches_the_pool(
    exploding_pool: ExplodingPool, priority
):
    """The same range `IssueService` publishes, reached through its constants.

    A client cannot tell which mutation refused it, so a bulk update reporting
    a different range for `priority` than a single update would be two
    contracts for one column.
    """
    service = bulk_service(exploding_pool)

    with pytest.raises(ValidationError) as raised:
        await service.update_many(
            scope=TEST_SCOPE,
            issue_ids=ISSUE_IDS,
            patch=BulkIssuePatch(priority=priority),
        )

    assert codes(raised.value) == ["OUT_OF_RANGE"]
    assert exploding_pool.acquire_count == 0


# --- the count comparison that makes a batch all-or-nothing -------------


async def test_a_batch_is_refused_when_an_id_does_not_resolve_here():
    """One id short from the scoped lock refuses the whole batch.

    This is the shape a foreign-workspace id produces: the lock is scoped to
    the caller's workspace, so an id belonging to somebody else simply does not
    come back, and the counts disagree. The fake returns two rows for three
    ids, which is that situation with none of the setup.
    """
    connection = FakeConnection(rows=[snapshot_row(id) for id in ISSUE_IDS[:2]])
    pool = FakePool(connection)

    with pytest.raises(ValidationError) as raised:
        await bulk_service(pool).update_many(
            scope=TEST_SCOPE,
            issue_ids=ISSUE_IDS,
            patch=BulkIssuePatch(priority=2),
        )

    assert codes(raised.value) == ["NOT_FOUND"]
    assert fields(raised.value) == ["issueIds"]


async def test_the_refusal_names_no_id():
    """A caller who could learn WHICH id was rejected could binary-search
    another tenant's issue ids at a hundred per request -- learning nothing
    about the issues and everything about which ids are real."""
    connection = FakeConnection(rows=[snapshot_row(ISSUE_IDS[0])])
    pool = FakePool(connection)

    with pytest.raises(ValidationError) as raised:
        await bulk_service(pool).update_many(
            scope=TEST_SCOPE,
            issue_ids=ISSUE_IDS,
            patch=BulkIssuePatch(priority=2),
        )

    for issue_id in ISSUE_IDS:
        assert str(issue_id) not in raised.value.issues[0].message


async def test_nothing_is_written_once_the_lock_comes_up_short():
    """The refusal happens before any write statement is issued.

    The rollback is what makes a batch atomic, and it is real -- see
    tests/test_bulk_actions_db.py. This asserts the cheaper half: the service
    does not issue the UPDATE at all, so there is nothing for the rollback to
    undo and no window in which a crash could leave it applied.
    """
    connection = FakeConnection(rows=[snapshot_row(ISSUE_IDS[0])])
    pool = FakePool(connection)

    with pytest.raises(ValidationError):
        await bulk_service(pool).update_many(
            scope=TEST_SCOPE,
            issue_ids=ISSUE_IDS,
            patch=BulkIssuePatch(priority=2),
        )

    assert len(connection.queries) == 1
    assert "FOR UPDATE" in normalize(connection.queries[0]["query"])


async def test_a_repeated_id_is_not_an_unauthorised_batch():
    """`[a, a, b]` names two issues.

    The comparison is against DISTINCT ids, so a client that sent a duplicate
    -- a double-click, a list built by concatenation -- is not told its batch
    contains an id it may not touch.
    """
    pool, _ = wired(locked=ISSUE_IDS[:2], updated=ISSUE_IDS[:2])

    await bulk_service(pool).update_many(
        scope=TEST_SCOPE,
        issue_ids=[ISSUE_IDS[0], ISSUE_IDS[0], ISSUE_IDS[1]],
        patch=BulkIssuePatch(priority=2),
    )


# --- the statements are scoped and parameterised ------------------------


async def test_every_statement_is_scoped_to_the_callers_workspace():
    """The tenant predicate is ANDed onto the id array, never folded into it.

    An id from another workspace therefore selects nothing rather than
    selecting that workspace's row. Asserted over the statement text because
    the property is about the SQL: a statement that took the ids first and
    checked the tenant afterwards would have already read the other tenant's
    rows.
    """
    pool, connection = wired(locked=ISSUE_IDS, updated=ISSUE_IDS)

    await bulk_service(pool).update_many(
        scope=TEST_SCOPE,
        issue_ids=ISSUE_IDS,
        patch=BulkIssuePatch(priority=2),
    )

    assert connection.queries

    for issued in connection.queries:
        # $1 is the workspace on every statement this batch issues, the
        # history inserts included -- so there is no statement in the whole
        # transaction that names an issue without naming the tenant beside it.
        assert issued["args"][0] == TEST_SCOPE.workspace_id

    for issued in connection.queries[:2]:
        # The two that READ or MOVE rows carry the predicate itself. The rest
        # are inserts, where the tenant is a column rather than a filter.
        assert "workspace_id = $1" in normalize(issued["query"])


async def test_the_lock_orders_by_id_so_two_batches_cannot_deadlock():
    """`UPDATE ... WHERE id = ANY(...)` locks rows in whatever order the plan
    produces, so two concurrent batches over overlapping selections can each
    hold a row the other wants. A total lock order taken up front is what makes
    the second one block instead."""
    pool, connection = wired(locked=ISSUE_IDS, updated=ISSUE_IDS)

    await bulk_service(pool).update_many(
        scope=TEST_SCOPE,
        issue_ids=ISSUE_IDS,
        patch=BulkIssuePatch(priority=2),
    )

    lock = normalize(connection.queries[0]["query"])

    assert "ORDER BY id FOR UPDATE" in lock


async def test_an_unset_field_is_not_written():
    """The two-parameter shape: a flag says whether to write, a value says what.

    A patch naming only the priority must leave every other column alone, and
    the flag for each of them arrives as False. Asserted on the arguments
    rather than on the SQL, because the statement text is the same either way
    -- which is the point of the CASE form.
    """
    pool, connection = wired(locked=ISSUE_IDS, updated=ISSUE_IDS)

    await bulk_service(pool).update_many(
        scope=TEST_SCOPE,
        issue_ids=ISSUE_IDS,
        patch=BulkIssuePatch(priority=2),
    )

    update = connection.queries[1]["args"]

    # $3 is the workflow-state flag, $7 the priority flag; only the second is
    # set. The positions are the statement's own and are asserted rather than
    # described, because a reordering that broke the pairing would otherwise
    # write one field's value into another field's column.
    assert update[2] is False
    assert update[6] is True
    assert update[7] == 2


async def test_a_patch_field_left_unset_stays_unset_through_the_transport():
    """`BulkIssuePatch` defaults every field to UNSET, so a patch that names
    nothing changes nothing -- the property `is_empty` reads and the reason a
    plain `None` default would make clearing an assignee unexpressible."""
    patch = BulkIssuePatch()

    assert patch.is_empty
    assert patch.assignee_id is UNSET
    assert not BulkIssuePatch(assignee_id=None).is_empty
