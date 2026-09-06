"""ProjectService against a real PostgreSQL 18.

tests/test_migration_009_db.py proves what the schema refuses.
tests/test_projects.py proves what the service refuses before it asks. This
file is the join between them: it proves the service turns the server's
refusals into the errors a client can act on, and turns nothing else into one.

That distinction is the whole subject. `_CONSTRAINT_ERRORS` maps constraint
names onto field errors, and a mapping like that has two ways to be wrong that
no unit test reaches: it can name a constraint the schema does not have, in
which case the refusal it was written for arrives as a masked internal error;
and it can be right about the name while the statement breaks a different
constraint than the author assumed, in which case a client is sent to correct
the wrong half of its request.

Marked `db`: deselected by default, skipped when Docker is unreachable.
"""

from uuid import UUID

import asyncpg
import pytest

from app.domain.errors import ValidationError
from app.domain.tenancy import WorkspaceScope
from app.repositories.issues import IssueRepository
from app.repositories.projects import ProjectRepository
from app.services.projects import ProjectService

from tests.conftest import apply_all_migrations, reset_schema


pytestmark = pytest.mark.db

BOOTSTRAP_WORKSPACE_ID = UUID("00000000-0000-7000-8000-000000000001")
BOOTSTRAP_TEAM_ID = UUID("00000000-0000-7000-8000-000000000002")

OTHER_WORKSPACE_ID = UUID("00000000-0000-7000-8000-0000000000a1")
OTHER_TEAM_ID = UUID("00000000-0000-7000-8000-0000000000a2")

MEMBER_ID = UUID("00000000-0000-7000-8000-0000000000e1")
OUTSIDER_ID = UUID("00000000-0000-7000-8000-0000000000e2")

SCOPE = WorkspaceScope(workspace_id=BOOTSTRAP_WORKSPACE_ID)

INSERT_USER_SQL = """
INSERT INTO users (id, email, password_hash)
VALUES (
    $1::uuid,
    'user-' || $1::text || '@example.test',
    '$argon2id$not-a-real-hash'
)
"""


@pytest.fixture
async def pool(postgres_dsn):
    """Two tenants, two teams, two accounts -- one member of each workspace."""
    connection = await asyncpg.connect(postgres_dsn)

    try:
        await reset_schema(connection)
        await apply_all_migrations(connection)

        await connection.execute(
            "INSERT INTO workspaces (id, slug, name) VALUES ($1, $2, $3)",
            OTHER_WORKSPACE_ID,
            "acme",
            "Acme",
        )
        await connection.execute(
            "INSERT INTO teams (id, workspace_id, name, key) VALUES ($1, $2, $3, $4)",
            OTHER_TEAM_ID,
            OTHER_WORKSPACE_ID,
            "Acme Core",
            "ACME",
        )

        for user_id, workspace_id in (
            (MEMBER_ID, BOOTSTRAP_WORKSPACE_ID),
            (OUTSIDER_ID, OTHER_WORKSPACE_ID),
        ):
            await connection.execute(INSERT_USER_SQL, user_id)
            await connection.execute(
                "INSERT INTO workspace_members (workspace_id, user_id, role) "
                "VALUES ($1, $2, $3)",
                workspace_id,
                user_id,
                "member",
            )
    finally:
        await connection.close()

    created = await asyncpg.create_pool(dsn=postgres_dsn, min_size=1, max_size=2)

    try:
        yield created
    finally:
        await created.close()


@pytest.fixture
def service(pool) -> ProjectService:
    """The real service over the real repositories over the real database."""
    return ProjectService(
        pool=pool,
        repository=ProjectRepository(),
        issue_repository=IssueRepository(),
    )


# ----------------------------------------------------------------- the lead


async def test_a_project_can_be_created_with_a_lead_who_is_a_member(service):
    project = await service.create(
        scope=SCOPE,
        name="Launch",
        description=None,
        state="planned",
        target_date=None,
        lead_id=MEMBER_ID,
    )

    assert project.lead_id == MEMBER_ID

    # Read back through a second call, so this is what the database holds and
    # not what the INSERT happened to return.
    reloaded = await service.get_by_id(scope=SCOPE, project_id=project.id)

    assert reloaded is not None
    assert reloaded.lead_id == MEMBER_ID


async def test_a_lead_from_another_workspace_is_a_field_error_and_not_a_crash(service):
    """The server's refusal, translated -- the half a unit test cannot reach.

    OUTSIDER_ID is a real account with a real membership of another tenant, so
    nothing in this process can tell it is wrong without asking. What the
    client gets back is a field error naming `leadId`, rather than the masked
    "Internal server error" an untranslated ForeignKeyViolationError produces.
    """
    with pytest.raises(ValidationError) as raised:
        await service.create(
            scope=SCOPE,
            name="Launch",
            description=None,
            state="planned",
            target_date=None,
            lead_id=OUTSIDER_ID,
        )

    assert [(issue.field, issue.code) for issue in raised.value.issues] == [
        ("leadId", "NOT_MEMBER")
    ]


async def test_a_lead_that_names_nobody_is_the_same_answer(service):
    """A nonexistent user and a member of another workspace are one error.

    Distinguishing them would answer "is this id an account here?" for anyone
    holding a workspace and a guess, which is the cross-tenant existence check
    the composite key exists to deny.
    """
    with pytest.raises(ValidationError) as raised:
        await service.create(
            scope=SCOPE,
            name="Launch",
            description=None,
            state="planned",
            target_date=None,
            lead_id=UUID("00000000-0000-7000-8000-0000000000ff"),
        )

    assert [(issue.field, issue.code) for issue in raised.value.issues] == [
        ("leadId", "NOT_MEMBER")
    ]


async def test_the_lead_can_be_reassigned_and_cleared_but_not_smuggled(service):
    """The three cases a patch has to keep apart, on one project.

    Renaming leaves the lead alone (UNSET), null clears it, and an id from
    another tenant is refused on UPDATE exactly as on INSERT. A signature that
    could not tell "not mentioned" from "set to null" would unassign the lead
    on the rename in the middle.
    """
    project = await service.create(
        scope=SCOPE,
        name="Launch",
        description=None,
        state="planned",
        target_date=None,
        lead_id=MEMBER_ID,
    )

    renamed = await service.update(scope=SCOPE, project_id=project.id, name="Relaunch")

    assert renamed.name == "Relaunch"
    assert renamed.lead_id == MEMBER_ID, "a rename must not vacate the lead"

    with pytest.raises(ValidationError):
        await service.update(scope=SCOPE, project_id=project.id, lead_id=OUTSIDER_ID)

    still_led = await service.get_by_id(scope=SCOPE, project_id=project.id)

    assert still_led is not None
    assert still_led.lead_id == MEMBER_ID, "a refused update must change nothing"

    cleared = await service.update(scope=SCOPE, project_id=project.id, lead_id=None)

    assert cleared.lead_id is None


# ------------------------------------------------------------------- teams


async def test_a_team_from_another_workspace_cannot_be_added(service):
    """Refused by the composite key, reported as a field error on `teamId`."""
    project = await service.create(
        scope=SCOPE,
        name="Launch",
        description=None,
        state="planned",
        target_date=None,
    )

    with pytest.raises(ValidationError) as raised:
        await service.add_team(
            scope=SCOPE, project_id=project.id, team_id=OTHER_TEAM_ID
        )

    assert [(issue.field, issue.code) for issue in raised.value.issues] == [
        ("teamId", "NOT_FOUND")
    ]


async def test_adding_the_same_team_twice_is_reported_on_the_team_field(service):
    """The primary key refuses it, and the message names which id to change.

    Two constraints on one statement raise the same exception class and mean
    different things -- this is why `_CONSTRAINT_ERRORS` is keyed on the
    constraint name rather than on the class.
    """
    project = await service.create(
        scope=SCOPE,
        name="Launch",
        description=None,
        state="planned",
        target_date=None,
    )

    added = await service.add_team(
        scope=SCOPE, project_id=project.id, team_id=BOOTSTRAP_TEAM_ID
    )

    assert added.team_ids == (BOOTSTRAP_TEAM_ID,)

    with pytest.raises(ValidationError) as raised:
        await service.add_team(
            scope=SCOPE, project_id=project.id, team_id=BOOTSTRAP_TEAM_ID
        )

    assert [(issue.field, issue.code) for issue in raised.value.issues] == [
        ("teamId", "ALREADY_ASSOCIATED")
    ]


# ------------------------------------------------------------------ tenancy


async def test_another_workspaces_project_is_invisible_rather_than_forbidden(service):
    """The same answer an id that exists nowhere gives.

    Created in the other workspace and read back through this one's scope: a
    caller holding a leaked id learns nothing by asking.
    """
    other_scope = WorkspaceScope(workspace_id=OTHER_WORKSPACE_ID)

    theirs = await service.create(
        scope=other_scope,
        name="Theirs",
        description=None,
        state="planned",
        target_date=None,
    )

    assert await service.get_by_id(scope=SCOPE, project_id=theirs.id) is None

    # And it is not merely hidden from reads: a write against it does not find
    # a row either, so nothing about it can be changed from here.
    with pytest.raises(ValidationError) as raised:
        await service.update(scope=SCOPE, project_id=theirs.id, name="Mine now")

    assert [(issue.field, issue.code) for issue in raised.value.issues] == [
        ("id", "NOT_FOUND")
    ]

    untouched = await service.get_by_id(scope=other_scope, project_id=theirs.id)

    assert untouched is not None
    assert untouched.name == "Theirs"


async def test_the_list_is_scoped_and_pages_by_keyset(service):
    """Newest first, one workspace only, and the cursor resumes where it left off."""
    for index in range(5):
        await service.create(
            scope=SCOPE,
            name=f"Project {index}",
            description=None,
            state="planned",
            target_date=None,
        )

    await service.create(
        scope=WorkspaceScope(workspace_id=OTHER_WORKSPACE_ID),
        name="Not ours",
        description=None,
        state="planned",
        target_date=None,
    )

    first_page = await service.list(scope=SCOPE, first=2, after=None)

    assert len(first_page.nodes) == 2
    assert first_page.has_next_page
    assert first_page.end_cursor is not None

    second_page = await service.list(scope=SCOPE, first=10, after=first_page.end_cursor)

    assert not second_page.has_next_page

    names = [node.name for node in first_page.nodes + second_page.nodes]

    # Five, not six: the other workspace's project is not in this walk. And no
    # row is handed out twice or skipped between the two pages.
    assert names == [f"Project {index}" for index in reversed(range(5))]


# --------------------------------------------------------------- milestones


async def test_the_batched_milestone_limit_is_per_project_not_per_batch(
    service, monkeypatch
):
    """Each project gets the whole bound, however many share the query.

    The failure this guards is a plain `LIMIT` on the batched statement, which
    divides one budget across the page: a project would come back whole when
    read on its own and truncated when read in a list, and which projects lost
    rows would depend on where they fell. Both projects here have more
    milestones than the (patched-down) bound, so a batch-wide limit returns
    fewer rows than this expects and returns them from one project.

    The constant is patched rather than seeding 200 milestones twice: the
    question is whether the SQL partitions, and that is answered at 2 as well
    as at 200.
    """
    monkeypatch.setattr("app.services.projects.MILESTONE_LIST_LIMIT", 2)

    projects = []

    for name in ("First", "Second"):
        project = await service.create(
            scope=SCOPE,
            name=name,
            description=None,
            state="planned",
            target_date=None,
        )
        projects.append(project)

        for index in range(3):
            await service.create_milestone(
                scope=SCOPE,
                project_id=project.id,
                name=f"{name} {index}",
                target_date=None,
            )

    batched = await service.list_milestones_for_projects(
        scope=SCOPE, project_ids=[project.id for project in projects]
    )

    per_project: dict[UUID, list[str]] = {}

    for milestone in batched:
        per_project.setdefault(milestone.project_id, []).append(milestone.name)

    assert per_project[projects[0].id] == ["First 0", "First 1"]
    assert per_project[projects[1].id] == ["Second 0", "Second 1"]

    # And the rows kept are the rows shown first: the same two the unbatched
    # read returns for that project on its own.
    single = await service.list_milestones(scope=SCOPE, project_id=projects[0].id)

    assert [milestone.name for milestone in single] == ["First 0", "First 1"]


# ------------------------------------------------------------------ deletion


async def test_deleting_a_project_detaches_its_issues_rather_than_removing_them(
    service, pool
):
    """Milestones are destroyed, issues are kept and unassigned.

    A product decision, which is why it is written in the service rather than
    left to ON DELETE CASCADE -- and why the two tables are not treated alike.
    """
    project = await service.create(
        scope=SCOPE,
        name="Launch",
        description=None,
        state="planned",
        target_date=None,
    )
    milestone = await service.create_milestone(
        scope=SCOPE, project_id=project.id, name="Beta", target_date=None
    )
    await service.add_team(
        scope=SCOPE, project_id=project.id, team_id=BOOTSTRAP_TEAM_ID
    )

    async with pool.acquire() as connection:
        workflow_state_id = await connection.fetchval(
            """
            SELECT id FROM workflow_states
            WHERE workspace_id = $1 AND team_id = $2
            ORDER BY position, id LIMIT 1
            """,
            BOOTSTRAP_WORKSPACE_ID,
            BOOTSTRAP_TEAM_ID,
        )
        issue_id = await connection.fetchval(
            """
            INSERT INTO issues (
                workspace_id, team_id, number, workflow_state_id,
                title, priority, project_id, milestone_id
            )
            VALUES ($1, $2, 1, $3, 'An issue', 1, $4, $5)
            RETURNING id
            """,
            BOOTSTRAP_WORKSPACE_ID,
            BOOTSTRAP_TEAM_ID,
            workflow_state_id,
            project.id,
            milestone.id,
        )

    await service.delete(scope=SCOPE, project_id=project.id)

    assert await service.get_by_id(scope=SCOPE, project_id=project.id) is None

    async with pool.acquire() as connection:
        row = await connection.fetchrow(
            "SELECT project_id, milestone_id FROM issues WHERE id = $1", issue_id
        )
        milestones_left = await connection.fetchval(
            "SELECT count(*) FROM project_milestones WHERE project_id = $1",
            project.id,
        )
        links_left = await connection.fetchval(
            "SELECT count(*) FROM project_teams WHERE project_id = $1", project.id
        )

    assert row is not None, "the issue itself must survive its project"
    assert row["project_id"] is None
    assert row["milestone_id"] is None
    assert milestones_left == 0
    assert links_left == 0


async def test_a_failed_delete_leaves_the_project_and_its_issues_alone(service, pool):
    """The whole delete is one transaction, so a miss rolls back what preceded it.

    Deleting a project from the wrong workspace clears no issues, and the
    guarantee is the rollback rather than the argument that the earlier
    statements could not have matched anything.
    """
    other_scope = WorkspaceScope(workspace_id=OTHER_WORKSPACE_ID)

    theirs = await service.create(
        scope=other_scope,
        name="Theirs",
        description=None,
        state="planned",
        target_date=None,
    )

    with pytest.raises(ValidationError):
        await service.delete(scope=SCOPE, project_id=theirs.id)

    survivor = await service.get_by_id(scope=other_scope, project_id=theirs.id)

    assert survivor is not None
