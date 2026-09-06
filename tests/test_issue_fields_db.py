"""The 006 issue fields, driven through the real service by a real server.

`test_issue_repository_writes.py` pins the SQL these paths send to a fake
connection. That is evidence about the statement's text and none at all about
its behaviour, and three of the things this branch adds are behaviour no fake
can show:

  * The patch statement is one UPDATE in which every field is
    `CASE WHEN <flag> THEN <value> ELSE <column> END`. Whether that actually
    leaves unmentioned columns alone -- and actually clears the ones a request
    set to null -- is a question about how PostgreSQL evaluates seventeen
    bound parameters against a row it holds, not about the string.

  * `completed_at` is derived from the issue's workflow state on every write,
    by a correlated subquery the fake connection never runs. Its refinements
    (Done to Canceled keeps the original instant; leaving a terminal state
    clears it) are each a different value coming back from the server.

  * Archival is a predicate -- `archived_at IS NULL` -- repeated across every
    read and write here. That an archived issue then becomes invisible to
    *all* of them, uniformly, is a property of six statements agreeing, which
    only running all six can establish.

The suite's subject is the application, so it applies the whole migration
chain rather than a prefix; see `apply_all_migrations`.

Marked `db`: deselected by default, skipped when Docker is unreachable.
"""

from datetime import date
from uuid import UUID

import asyncpg
import pytest

from app.domain.errors import ValidationError
from app.domain.issues import UNSET, IssuePatch
from app.domain.tenancy import WorkspaceScope
from app.repositories.issues import IssueRepository
from app.repositories.teams import TeamRepository
from app.services.issues import IssueService
from app.services.teams import TeamService

from tests.conftest import apply_all_migrations, reset_schema


pytestmark = pytest.mark.db

BOOTSTRAP_WORKSPACE_ID = UUID("00000000-0000-7000-8000-000000000001")
BOOTSTRAP_TEAM_ID = UUID("00000000-0000-7000-8000-000000000002")

# 005 gives the bootstrap team this key, so an issue's identifier is CORE-n.
BOOTSTRAP_TEAM_KEY = "CORE"

OTHER_WORKSPACE_ID = UUID("00000000-0000-7000-8000-0000000000b1")

MEMBER_ID = UUID("00000000-0000-7000-8000-0000000000c1")
OUTSIDER_ID = UUID("00000000-0000-7000-8000-0000000000c2")

PASSWORD_HASH = "$argon2id$not-a-real-hash"

SCOPE = WorkspaceScope(workspace_id=BOOTSTRAP_WORKSPACE_ID)


@pytest.fixture
async def service(postgres_dsn):
    """The real service over the real repository over a real PostgreSQL 18.

    The container is session-scoped, so the schema is dropped first. Two
    users exist: one a member of the bootstrap workspace, one a member only
    of another -- which is what makes a cross-tenant assignment expressible.
    """
    connection = await asyncpg.connect(postgres_dsn)

    try:
        await reset_schema(connection)
        await apply_all_migrations(connection)

        await connection.execute(
            "INSERT INTO workspaces (id, slug, name) VALUES ($1, 'other', 'Other')",
            OTHER_WORKSPACE_ID,
        )
        await connection.executemany(
            "INSERT INTO users (id, email, password_hash) VALUES ($1, $2, $3)",
            [
                (MEMBER_ID, "member@vector.test", PASSWORD_HASH),
                (OUTSIDER_ID, "outsider@vector.test", PASSWORD_HASH),
            ],
        )
        await connection.executemany(
            """
            INSERT INTO workspace_members (workspace_id, user_id, role)
            VALUES ($1, $2, 'member')
            """,
            [
                (BOOTSTRAP_WORKSPACE_ID, MEMBER_ID),
                (OTHER_WORKSPACE_ID, OUTSIDER_ID),
            ],
        )
    finally:
        await connection.close()

    pool = await asyncpg.create_pool(dsn=postgres_dsn, min_size=1, max_size=2)

    try:
        yield IssueService(
            pool=pool,
            repository=IssueRepository(),
            teams=TeamService(pool=pool, repository=TeamRepository()),
        )
    finally:
        await pool.close()


@pytest.fixture
async def states(postgres_dsn) -> dict[str, UUID]:
    """The bootstrap team's board, keyed by category rather than by name.

    By category on purpose, and it is the same discipline 005 asks of the
    application: 'Done' is a label a team may rename, 'completed' is a
    category its CHECK constrains.
    """
    connection = await asyncpg.connect(postgres_dsn)

    try:
        rows = await connection.fetch(
            """
            SELECT type, id FROM workflow_states
            WHERE workspace_id = $1 AND team_id = $2
            """,
            BOOTSTRAP_WORKSPACE_ID,
            BOOTSTRAP_TEAM_ID,
        )
    finally:
        await connection.close()

    return {row["type"]: row["id"] for row in rows}


async def _file(service: IssueService, **fields):
    return await service.create(
        scope=SCOPE, team_id=BOOTSTRAP_TEAM_ID, title="Ship it", **fields
    )


async def test_a_created_issue_renders_the_identifier_it_is_known_by(service):
    """CORE-1, from the team's key and the number the counter handed out.

    This is the one field the product shows that neither `teams.key` nor
    `issues.number` reaches on its own, and the repository builds it from a
    correlated subquery that no fake connection resolves.
    """
    first = await _file(service)
    second = await _file(service)

    assert (first.number, second.number) == (1, 2)
    assert first.identifier == f"{BOOTSTRAP_TEAM_KEY}-1"
    assert second.identifier == f"{BOOTSTRAP_TEAM_KEY}-2"

    # And it survives the round trip through a read, not just the RETURNING.
    fetched = await service.get_by_id(scope=SCOPE, issue_id=first.id)

    assert fetched is not None
    assert fetched.identifier == f"{BOOTSTRAP_TEAM_KEY}-1"


async def test_a_new_issue_starts_unstarted_and_not_complete(service, states):
    issue = await _file(service)

    assert issue.workflow_state_id == states["unstarted"]
    assert issue.completed_at is None
    assert issue.archived_at is None


async def test_the_006_fields_survive_a_create_and_a_read(service):
    issue = await _file(
        service,
        description="described",
        priority=2,
        assignee_id=MEMBER_ID,
        creator_id=MEMBER_ID,
        estimate=5,
        due_date=date(2026, 3, 14),
    )

    fetched = await service.get_by_id(scope=SCOPE, issue_id=issue.id)

    assert fetched == issue
    assert fetched is not None
    assert fetched.assignee_id == MEMBER_ID
    assert fetched.creator_id == MEMBER_ID
    assert fetched.estimate == 5
    assert fetched.due_date == date(2026, 3, 14)


async def test_assigning_someone_from_another_workspace_is_a_field_error(service):
    """The composite key refuses it, and the service reports it as input.

    OUTSIDER_ID is a real user and a real member -- of somewhere else -- so
    this is the cross-tenant assignment `issues_assignee_fk` exists to
    refuse. It reaches the service as a ForeignKeyViolationError and must
    leave as a ValidationError naming the field, because it is a value the
    client chose and can correct.
    """
    with pytest.raises(ValidationError) as exc_info:
        await _file(service, assignee_id=OUTSIDER_ID)

    assert [(issue.field, issue.code) for issue in exc_info.value.issues] == [
        ("assigneeId", "NOT_A_MEMBER")
    ]


async def test_a_failed_create_returns_its_number_to_the_counter(service):
    """The gaplessness contract 005 states, checked at the one place it can break.

    The allocation and the insert share a transaction, so a rejected assignee
    must take the increment down with it. Were the number burnt, the next
    issue would be CORE-2 with no CORE-1 -- a permanent hole punched by an
    ordinary user mistake.
    """
    with pytest.raises(ValidationError):
        await _file(service, assignee_id=OUTSIDER_ID)

    issue = await _file(service)

    assert issue.number == 1


async def test_a_patch_changes_only_the_fields_it_names(service):
    issue = await _file(
        service, description="described", priority=2, estimate=5, assignee_id=MEMBER_ID
    )

    updated = await service.update(
        scope=SCOPE, issue_id=issue.id, patch=IssuePatch(title="Renamed")
    )

    assert updated is not None
    assert updated.title == "Renamed"

    # Everything the patch did not mention is untouched. The ELSE branch of
    # each CASE is what this asserts, and a fake connection cannot show it.
    assert updated.description == "described"
    assert updated.priority == 2
    assert updated.estimate == 5
    assert updated.assignee_id == MEMBER_ID
    assert updated.number == issue.number


async def test_null_clears_a_field_and_omission_preserves_it(service):
    """The distinction the whole UNSET sentinel exists for.

    Both requests below carry a `None` for assignee in the sense that a
    single nullable parameter would: one means "unassign", the other means "I
    am editing the estimate". Collapsing them is what leaves a product unable
    to unassign anything.
    """
    issue = await _file(service, assignee_id=MEMBER_ID, estimate=5)

    untouched = await service.update(
        scope=SCOPE, issue_id=issue.id, patch=IssuePatch(estimate=8)
    )

    assert untouched is not None
    assert untouched.assignee_id == MEMBER_ID, "an unmentioned field must survive"

    cleared = await service.update(
        scope=SCOPE, issue_id=issue.id, patch=IssuePatch(assignee_id=None)
    )

    assert cleared is not None
    assert cleared.assignee_id is None
    assert cleared.estimate == 8, "clearing one field must not clear another"


async def test_moving_into_a_terminal_state_stamps_completed_at(service, states):
    issue = await _file(service)

    assert issue.completed_at is None

    done = await service.update(
        scope=SCOPE,
        issue_id=issue.id,
        patch=IssuePatch(workflow_state_id=states["completed"]),
    )

    assert done is not None
    assert done.completed_at is not None


async def test_leaving_a_terminal_state_clears_completed_at(service, states):
    issue = await _file(service)

    await service.update(
        scope=SCOPE,
        issue_id=issue.id,
        patch=IssuePatch(workflow_state_id=states["completed"]),
    )

    reopened = await service.update(
        scope=SCOPE,
        issue_id=issue.id,
        patch=IssuePatch(workflow_state_id=states["started"]),
    )

    assert reopened is not None
    assert reopened.completed_at is None


async def test_moving_between_two_terminal_states_keeps_the_original_instant(
    service, states
):
    """Done to Canceled relabels why the work stopped; it did not restart.

    `COALESCE(completed_at, now())` is what produces this, and it is the one
    refinement of the rule that a naive `WHEN terminal THEN now()` would get
    wrong while passing every other test in this file.
    """
    issue = await _file(service)

    done = await service.update(
        scope=SCOPE,
        issue_id=issue.id,
        patch=IssuePatch(workflow_state_id=states["completed"]),
    )

    canceled = await service.update(
        scope=SCOPE,
        issue_id=issue.id,
        patch=IssuePatch(workflow_state_id=states["canceled"]),
    )

    assert done is not None
    assert canceled is not None
    assert canceled.workflow_state_id == states["canceled"]
    assert canceled.completed_at == done.completed_at


async def test_a_patch_cannot_move_an_issue_into_another_teams_state(service):
    """The state has to belong to the issue's own team, in its own workspace.

    Enforced by `issues_workflow_state_fk` as part of the UPDATE, never by a
    SELECT beforehand -- and translated to a field error because the id came
    from the client.
    """
    issue = await _file(service)

    with pytest.raises(ValidationError) as exc_info:
        await service.update(
            scope=SCOPE,
            issue_id=issue.id,
            patch=IssuePatch(workflow_state_id=UUID(int=999)),
        )

    assert [(item.field, item.code) for item in exc_info.value.issues] == [
        ("workflowStateId", "NOT_FOUND")
    ]


async def test_an_empty_patch_is_refused_rather_than_stamping_updated_at(service):
    issue = await _file(service)

    with pytest.raises(ValidationError) as exc_info:
        await service.update(scope=SCOPE, issue_id=issue.id, patch=IssuePatch())

    assert [(item.field, item.code) for item in exc_info.value.issues] == [
        ("input", "EMPTY")
    ]

    unchanged = await service.get_by_id(scope=SCOPE, issue_id=issue.id)

    assert unchanged is not None
    assert unchanged.updated_at == issue.updated_at


async def test_archiving_removes_the_issue_from_every_read(service):
    """One predicate, six statements, and this is the assertion that they agree."""
    kept = await _file(service)
    doomed = await _file(service)

    archived = await service.archive(scope=SCOPE, issue_id=doomed.id)

    assert archived is not None
    assert archived.id == doomed.id
    assert archived.archived_at is not None

    assert await service.get_by_id(scope=SCOPE, issue_id=doomed.id) is None

    page = await service.list(scope=SCOPE, first=50, after=None)

    assert [node.id for node in page.nodes] == [kept.id]


async def test_the_archived_row_is_still_there_holding_its_number(
    service, postgres_dsn
):
    """Archived, not deleted, and this is the difference that matters.

    005 makes `number` gapless and never reissues it, so a hard DELETE would
    turn CORE-1 into a name that resolves to nothing everywhere it was
    already written down -- URLs, commit messages, conversation.
    """
    issue = await _file(service)

    await service.archive(scope=SCOPE, issue_id=issue.id)

    connection = await asyncpg.connect(postgres_dsn)

    try:
        row = await connection.fetchrow(
            "SELECT number, archived_at FROM issues WHERE id = $1", issue.id
        )
    finally:
        await connection.close()

    assert row is not None, "archiving must not delete the row"
    assert row["number"] == issue.number
    assert row["archived_at"] is not None


async def test_archiving_twice_answers_nothing_the_second_time(service):
    """Which keeps `archived_at` the moment of archival, not of the last attempt.

    None here is the same answer a nonexistent id gets, deliberately: an
    archived issue is invisible to every read, so a caller who cannot see it
    must not be able to learn it exists by trying to archive it.
    """
    issue = await _file(service)

    first = await service.archive(scope=SCOPE, issue_id=issue.id)
    second = await service.archive(scope=SCOPE, issue_id=issue.id)

    assert first is not None
    assert second is None


async def test_an_archived_issue_is_not_there_to_update(service):
    issue = await _file(service)

    await service.archive(scope=SCOPE, issue_id=issue.id)

    result = await service.update(
        scope=SCOPE, issue_id=issue.id, patch=IssuePatch(title="Renamed")
    )

    assert result is None


async def test_another_tenants_issue_is_indistinguishable_from_a_missing_one(service):
    """Every operation, not just the reads.

    An update or an archive that reported a different kind of failure for
    "exists but is not yours" would be a probe: a caller holding a guessed id
    could enumerate another workspace's issues one request at a time.
    """
    issue = await _file(service)
    elsewhere = WorkspaceScope(workspace_id=OTHER_WORKSPACE_ID)

    assert await service.get_by_id(scope=elsewhere, issue_id=issue.id) is None
    assert await service.archive(scope=elsewhere, issue_id=issue.id) is None
    assert (
        await service.update(
            scope=elsewhere, issue_id=issue.id, patch=IssuePatch(title="Renamed")
        )
        is None
    )

    # And the issue is untouched in the workspace that does own it.
    mine = await service.get_by_id(scope=SCOPE, issue_id=issue.id)

    assert mine is not None
    assert mine.title == "Ship it"
    assert mine.archived_at is None


def test_the_patch_default_is_unset_for_every_field():
    """The premise every test above rests on, checked rather than assumed.

    A field that defaulted to None instead of UNSET would make every patch
    that omitted it a request to clear it, and the tests above would still
    pass -- they name the fields they assert about.
    """
    patch = IssuePatch()

    assert patch.is_empty
    assert all(
        value is UNSET
        for value in (
            patch.title,
            patch.description,
            patch.priority,
            patch.workflow_state_id,
            patch.assignee_id,
            patch.estimate,
            patch.due_date,
        )
    )
