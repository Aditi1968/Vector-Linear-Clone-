"""Teams, workflow states and number allocation, below the database.

Four levels of the same feature, each catching something the others cannot:

  * **A** pins the domain vocabulary against the migration's own CHECK
    constraint, by reading the SQL. A category the enum knows and the schema
    does not is a write that fails at runtime; the reverse is a state the
    application cannot read back. Neither is visible from either file alone.
  * **B** reads the statements the repository actually sends. A tenant
    predicate missing from a WHERE clause is invisible to every test that
    only checks what came back, because a fake connection agrees with
    whatever SQL it is handed -- and so does a database holding one tenant.
  * **C** is the service: what it does with an absence, what it acquires, and
    what it deliberately does not acquire.
  * **D** is the GraphQL boundary: what an unknown workspace slug discloses,
    and that the resolver never reaches the team service without a resolved
    scope.

None of this reaches PostgreSQL. `test_migration_005_db.py` and
`test_issue_number_concurrency_db.py` are the evidence about the server; this
file is the evidence about the code that talks to it.
"""

import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest

from app.domain.errors import TeamNotFoundError, WorkspaceAccessDeniedError
from app.domain.teams import (
    TeamEntity,
    TeamWorkflow,
    WorkflowStateCategory,
    WorkflowStateEntity,
)
from app.domain.tenancy import WorkspaceScope
from app.graphql.schema import build_schema
from app.graphql.scope import WORKSPACE_NOT_FOUND_MESSAGE
from app.repositories.teams import TeamRepository
from app.services.teams import TeamService

from tests.conftest import (
    TEST_USER_ID,
    TEST_WORKSPACE_SLUG,
    ExplodingPool,
    FakeConnection,
    FakeMembershipService,
    FakePool,
    graphql_context,
    normalize,
)


MIGRATION_005 = (
    Path(__file__).resolve().parents[1] / "migrations" / "005_team_workflows.sql"
)

WORKSPACE_ID = UUID("00000000-0000-7000-8000-000000000001")
TEAM_ID = UUID("00000000-0000-7000-8000-000000000002")
OTHER_TEAM_ID = UUID("00000000-0000-7000-8000-0000000000b2")
STATE_ID = UUID("00000000-0000-7000-8000-0000000000c1")

SCOPE = WorkspaceScope(workspace_id=WORKSPACE_ID)

CREATED_AT = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

# Built directly rather than imported, so these tests need no DATABASE_URL.
schema = build_schema("test")

TEAMS_QUERY = """
query ListTeams($slug: String!) {
  teams(workspaceSlug: $slug) {
    id
    key
    name

    workflowStates {
      id
      name
      category
      position
      color
    }
  }
}
"""


def team_record(key: str = "CORE", team_id: UUID = TEAM_ID) -> dict:
    """asyncpg.Record supports __getitem__, which a dict models well enough."""
    return {
        "id": team_id,
        "workspace_id": WORKSPACE_ID,
        "key": key,
        "name": "Core",
        "created_at": CREATED_AT,
    }


def state_record(category: str = "unstarted", position: int = 1) -> dict:
    return {
        "id": STATE_ID,
        "workspace_id": WORKSPACE_ID,
        "team_id": TEAM_ID,
        "name": "Todo",
        "category": category,
        "position": position,
        "color": "#e2e2e2",
        "created_at": CREATED_AT,
    }


def team_entity(key: str = "CORE", team_id: UUID = TEAM_ID) -> TeamEntity:
    return TeamEntity(
        id=team_id,
        workspace_id=WORKSPACE_ID,
        key=key,
        name="Core",
        created_at=CREATED_AT,
    )


def state_entity(
    category: WorkflowStateCategory = WorkflowStateCategory.UNSTARTED,
    *,
    team_id: UUID = TEAM_ID,
    position: int = 1,
    name: str = "Todo",
) -> WorkflowStateEntity:
    return WorkflowStateEntity(
        id=UUID(int=position + 1),
        workspace_id=WORKSPACE_ID,
        team_id=team_id,
        name=name,
        category=category,
        position=position,
        color=None,
        created_at=CREATED_AT,
    )


class FakeTeamRepository:
    """Returns canned entities and records what it was called with."""

    def __init__(self, teams=None, states=None, number=None):
        self.teams = teams if teams is not None else []
        self.states = states if states is not None else []
        self.number = number
        self.calls: list[dict] = []

    async def list_by_workspace(self, connection, workspace_id):
        self.calls.append({"call": "list_by_workspace", "workspace_id": workspace_id})

        return list(self.teams)

    async def list_workflow_states(self, connection, workspace_id, team_ids):
        self.calls.append(
            {
                "call": "list_workflow_states",
                "workspace_id": workspace_id,
                "team_ids": list(team_ids),
            }
        )

        return list(self.states)

    async def allocate_issue_number(self, connection, workspace_id, team_id):
        self.calls.append(
            {
                "call": "allocate_issue_number",
                "workspace_id": workspace_id,
                "team_id": team_id,
            }
        )

        return self.number


def Context(membership_service, team_service):
    """The real context, wired to the two collaborators `teams` reaches.

    The workspace service is no longer one of them. `teams` used to resolve a
    slug to a workspace and stop there, which answered any caller who could
    spell the slug; it now goes through `app.graphql.scope.authorized_scope`,
    which resolves the viewer first and the MEMBERSHIP second -- so the seam
    a test replaces is the membership lookup.
    """
    return graphql_context(
        membership_service=membership_service,
        team_service=team_service,
    )


class RefusingMembershipService:
    """Refuses every slug, the way the real one refuses a non-member.

    One answer for a slug no workspace holds and for a workspace this user is
    not in, because the real lookup is a single statement that cannot tell
    them apart.
    """

    def __init__(self):
        self.slugs: list[str] = []

    async def authorized_scope_for_slug(self, *, slug, user_id):
        self.slugs.append(slug)

        raise WorkspaceAccessDeniedError()


class FakeTeamService:
    def __init__(self, workflows=None):
        self.workflows = workflows if workflows is not None else []
        self.scopes: list[WorkspaceScope] = []

    async def list_workflows(self, scope: WorkspaceScope):
        self.scopes.append(scope)

        return list(self.workflows)


class ExplodingTeamService:
    """Fails if the resolver reaches it at all."""

    async def list_workflows(self, scope):
        raise AssertionError(
            "the team service must not be reached without a resolved workspace"
        )


# --------------------------------------------------------------------------
# A. The vocabulary, checked against the migration that constrains it
# --------------------------------------------------------------------------


def _categories_in_migration() -> set[str]:
    """The values `workflow_states_type_check` admits, read out of the SQL.

    Parsed from the file rather than restated here, because a constant copied
    into a test is a copy of the same assumption the code makes: both would be
    edited together and neither would notice the schema disagreeing.
    """
    sql = MIGRATION_005.read_text(encoding="utf-8")
    check = re.search(
        r"CONSTRAINT workflow_states_type_check\s*CHECK \(type IN \(([^)]*)\)\)",
        sql,
    )

    assert check is not None, (
        "workflow_states_type_check is no longer written in a form this test "
        "can read; it is the thing being compared, so update the pattern "
        "rather than deleting the test"
    )

    return set(re.findall(r"'([^']*)'", check.group(1)))


def test_the_category_enum_and_the_check_constraint_admit_the_same_values():
    """The two halves of one vocabulary, which live in two files.

    A category in the enum but not the CHECK is an INSERT that fails at
    runtime, in production, on the one code path that adds it. A category in
    the CHECK but not the enum is a row the repository refuses to convert --
    `WorkflowStateCategory(row["category"])` raises -- so an existing issue
    becomes unreadable. Both are silent until they are not.
    """
    assert {category.value for category in WorkflowStateCategory} == (
        _categories_in_migration()
    )


def test_canceled_is_spelled_with_one_l_everywhere():
    """Pinned because two spellings of this word is the classic version of
    this defect, and because it reads as correct to whoever wrote either."""
    assert WorkflowStateCategory.CANCELED.value == "canceled"
    assert "cancelled" not in MIGRATION_005.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# B. The statements the repository sends
# --------------------------------------------------------------------------


async def test_listing_teams_is_scoped_to_the_workspace_and_ordered_by_key():
    connection = FakeConnection(rows=[team_record()])

    await TeamRepository().list_by_workspace(connection, WORKSPACE_ID)

    sent = connection.queries[0]

    assert normalize(sent["query"]) == normalize(
        """
        SELECT id, workspace_id, key, name, created_at
        FROM teams
        WHERE workspace_id = $1
        ORDER BY key
        """
    )
    assert sent["args"] == (WORKSPACE_ID,)


async def test_finding_a_team_by_key_compares_exactly_and_binds_both_values():
    """No lower(), no upper(), no LIKE, and the key never interpolated.

    `teams_key_format` confines every stored key to uppercase, so a key
    differing only in case is not a team this database can hold. Folding it
    onto a real team would make team addressing case-insensitive, which is
    what that constraint exists to prevent.
    """
    connection = FakeConnection(row=team_record())

    await TeamRepository().find_by_key(connection, WORKSPACE_ID, "eng")

    sent = connection.queries[0]
    statement = normalize(sent["query"])

    assert statement == normalize(
        """
        SELECT id, workspace_id, key, name, created_at
        FROM teams
        WHERE workspace_id = $1 AND key = $2
        """
    )
    assert sent["args"] == (WORKSPACE_ID, "eng")
    assert "eng" not in statement


async def test_a_missing_team_is_none_rather_than_an_exception():
    assert await TeamRepository().find_by_key(
        FakeConnection(), WORKSPACE_ID, "ENG"
    ) is (None)


async def test_workflow_states_are_batched_workspace_scoped_and_totally_ordered():
    """One statement for every team, and an ORDER BY with no ties left in it.

    `position` is deliberately not unique per team (005 leaves reordering a
    single statement), so `ORDER BY team_id, position` alone would let two
    states swap places between reads.
    """
    connection = FakeConnection(rows=[state_record()])

    await TeamRepository().list_workflow_states(
        connection,
        WORKSPACE_ID,
        [TEAM_ID, OTHER_TEAM_ID],
    )

    sent = connection.queries[0]

    assert normalize(sent["query"]) == normalize(
        """
        SELECT id, workspace_id, team_id, name, type AS category, position,
               color, created_at
        FROM workflow_states
        WHERE workspace_id = $1 AND team_id = ANY($2::UUID[])
        ORDER BY team_id, position, id
        """
    )
    assert sent["args"] == (WORKSPACE_ID, [TEAM_ID, OTHER_TEAM_ID])


async def test_rows_are_converted_to_entities_and_no_record_escapes():
    """The repository's contract: entities out, never rows.

    A `Record` reaching a service or a resolver would work -- it subscripts
    like a dict -- right up until something outside the repository depended on
    a column name, at which point the SQL and its callers are coupled with
    nothing naming the coupling.
    """
    connection = FakeConnection(rows=[state_record(category="started", position=2)])

    states = await TeamRepository().list_workflow_states(
        connection,
        WORKSPACE_ID,
        [TEAM_ID],
    )

    assert states == [
        WorkflowStateEntity(
            id=STATE_ID,
            workspace_id=WORKSPACE_ID,
            team_id=TEAM_ID,
            name="Todo",
            category=WorkflowStateCategory.STARTED,
            position=2,
            color="#e2e2e2",
            created_at=CREATED_AT,
        )
    ]

    # Constructed through the enum rather than passed through as a string, so
    # a category the domain does not know about fails at the row that carries
    # it rather than flowing on as a str.
    assert states[0].category is WorkflowStateCategory.STARTED


async def test_a_category_the_domain_does_not_know_is_rejected_at_the_row():
    connection = FakeConnection(rows=[state_record(category="in_progress")])

    with pytest.raises(ValueError):
        await TeamRepository().list_workflow_states(connection, WORKSPACE_ID, [TEAM_ID])


async def test_allocation_is_one_update_returning_and_never_a_select_max():
    """The concurrency-critical statement, read rather than inferred.

    Three claims, and each is the difference between a correct allocator and
    one that duplicates numbers under load:

      * exactly one statement -- a SELECT followed by an UPDATE reads without
        a lock and computes the same successor in two transactions;
      * the arithmetic is in the UPDATE, not in Python, for the same reason;
      * `workspace_id` is in the predicate, so a team id from another tenant
        increments nothing.
    """
    connection = FakeConnection(value=7)

    number = await TeamRepository().allocate_issue_number(
        connection,
        WORKSPACE_ID,
        TEAM_ID,
    )

    assert number == 7
    assert len(connection.queries) == 1

    sent = connection.queries[0]

    assert normalize(sent["query"]) == normalize(
        """
        UPDATE teams
        SET issue_counter = issue_counter + 1
        WHERE workspace_id = $1 AND id = $2
        RETURNING issue_counter
        """
    )
    assert sent["args"] == (WORKSPACE_ID, TEAM_ID)
    assert "max(" not in normalize(sent["query"]).lower()


async def test_allocating_against_no_matching_team_returns_none():
    """The repository reports the absence; deciding what it means is the
    service's job, and conflating the two is how "no such team" and "not your
    team" end up as different answers to a client."""
    assert (
        await TeamRepository().allocate_issue_number(
            FakeConnection(),
            WORKSPACE_ID,
            TEAM_ID,
        )
        is None
    )


# --------------------------------------------------------------------------
# C. The service
# --------------------------------------------------------------------------


async def test_listing_workflows_groups_states_under_their_team():
    repository = FakeTeamRepository(
        teams=[team_entity(), team_entity(key="DES", team_id=OTHER_TEAM_ID)],
        states=[
            state_entity(WorkflowStateCategory.BACKLOG, position=0, name="Backlog"),
            state_entity(WorkflowStateCategory.UNSTARTED, position=1),
            state_entity(
                WorkflowStateCategory.STARTED,
                team_id=OTHER_TEAM_ID,
                position=2,
                name="Doing",
            ),
        ],
    )

    workflows = await TeamService(
        pool=FakePool(),
        repository=repository,
    ).list_workflows(SCOPE)

    assert [workflow.team.key for workflow in workflows] == ["CORE", "DES"]
    assert [state.category for state in workflows[0].workflow_states] == [
        WorkflowStateCategory.BACKLOG,
        WorkflowStateCategory.UNSTARTED,
    ]
    assert [state.category for state in workflows[1].workflow_states] == [
        WorkflowStateCategory.STARTED
    ]


async def test_a_team_with_no_states_gets_an_empty_tuple_not_a_missing_key():
    """A team whose states were all deleted still has to appear in the list.

    Grouping with a plain dict lookup would raise KeyError here, and dropping
    the team would make a misconfigured team invisible rather than visibly
    empty.
    """
    workflows = await TeamService(
        pool=FakePool(),
        repository=FakeTeamRepository(teams=[team_entity()], states=[]),
    ).list_workflows(SCOPE)

    assert workflows == [TeamWorkflow(team=team_entity(), workflow_states=())]


async def test_an_empty_workspace_costs_one_query_not_two():
    """`= ANY('{}')` matches nothing, so issuing it would be a round trip
    whose answer is already known."""
    repository = FakeTeamRepository(teams=[])

    assert (
        await TeamService(pool=FakePool(), repository=repository).list_workflows(SCOPE)
        == []
    )

    assert [call["call"] for call in repository.calls] == ["list_by_workspace"]


async def test_the_service_passes_the_scopes_workspace_and_the_teams_it_found():
    repository = FakeTeamRepository(teams=[team_entity()])

    await TeamService(pool=FakePool(), repository=repository).list_workflows(SCOPE)

    assert repository.calls == [
        {"call": "list_by_workspace", "workspace_id": WORKSPACE_ID},
        {
            "call": "list_workflow_states",
            "workspace_id": WORKSPACE_ID,
            "team_ids": [TEAM_ID],
        },
    ]


async def test_allocation_uses_the_callers_connection_and_acquires_nothing():
    """The exception to "the service owns acquisition", asserted so it cannot
    be tidied away.

    The number and the issue that uses it must be decided by one transaction.
    A service that acquired its own connection here would commit the increment
    before the insert was attempted and release the row lock that serialises
    concurrent allocation -- so `ExplodingPool` is the assertion: any acquire
    at all is a test failure.
    """
    pool = ExplodingPool()
    repository = FakeTeamRepository(number=3)

    number = await TeamService(pool=pool, repository=repository).allocate_issue_number(
        FakeConnection(),
        scope=SCOPE,
        team_id=TEAM_ID,
    )

    assert number == 3
    assert pool.acquire_count == 0
    assert repository.calls == [
        {
            "call": "allocate_issue_number",
            "workspace_id": WORKSPACE_ID,
            "team_id": TEAM_ID,
        }
    ]


async def test_allocating_for_an_unknown_or_foreign_team_raises_team_not_found():
    with pytest.raises(TeamNotFoundError):
        await TeamService(
            pool=ExplodingPool(),
            repository=FakeTeamRepository(number=None),
        ).allocate_issue_number(
            FakeConnection(),
            scope=SCOPE,
            team_id=TEAM_ID,
        )


# --------------------------------------------------------------------------
# D. The GraphQL boundary
# --------------------------------------------------------------------------


async def test_the_teams_query_returns_a_team_with_its_states():
    workflow = TeamWorkflow(
        team=team_entity(),
        workflow_states=(
            state_entity(WorkflowStateCategory.BACKLOG, position=0, name="Backlog"),
            state_entity(WorkflowStateCategory.CANCELED, position=4, name="Canceled"),
        ),
    )

    result = await schema.execute(
        TEAMS_QUERY,
        variable_values={"slug": TEST_WORKSPACE_SLUG},
        context_value=Context(
            membership_service=FakeMembershipService(scope=SCOPE),
            team_service=FakeTeamService([workflow]),
        ),
    )

    assert result.errors is None
    assert result.data == {
        "teams": [
            {
                "id": str(TEAM_ID),
                "key": "CORE",
                "name": "Core",
                "workflowStates": [
                    {
                        "id": str(UUID(int=1)),
                        "name": "Backlog",
                        "category": "BACKLOG",
                        "position": 0,
                        "color": None,
                    },
                    {
                        "id": str(UUID(int=5)),
                        "name": "Canceled",
                        "category": "CANCELED",
                        "position": 4,
                        "color": None,
                    },
                ],
            }
        ]
    }


async def test_the_resolver_hands_the_service_a_resolved_scope_not_the_slug():
    team_service = FakeTeamService([])
    membership_service = FakeMembershipService(scope=SCOPE)

    result = await schema.execute(
        TEAMS_QUERY,
        variable_values={"slug": TEST_WORKSPACE_SLUG},
        context_value=Context(membership_service, team_service),
    )

    assert result.errors is None

    # The slug came from the document and the user id from the session. Both
    # halves matter: a resolver that read the user from an argument would be
    # an impersonation API, and one that hardcoded the slug would be the
    # bootstrap tenant all over again.
    assert membership_service.calls == [
        {"slug": TEST_WORKSPACE_SLUG, "user_id": TEST_USER_ID}
    ]
    assert team_service.scopes == [SCOPE]


async def test_a_workspace_the_viewer_may_not_see_is_not_found_and_reads_nothing():
    """The disclosure decision, pinned -- now with a membership behind it.

    This query used to answer an unknown slug with an empty list, because
    membership did not exist and an error would have separated "no such
    workspace" from "not yours". Both now answer with one NOT_FOUND carrying
    one fixed message, which is the same non-disclosure by a different route:
    the caller learns nothing about whether the workspace is real.

    The team service is one that fails if it is touched, so this also pins
    the ordering -- no protected read happens for a caller who was refused.
    """
    membership_service = RefusingMembershipService()

    result = await schema.execute(
        TEAMS_QUERY,
        variable_values={"slug": "nope"},
        context_value=Context(
            membership_service=membership_service,
            team_service=ExplodingTeamService(),
        ),
    )

    assert result.data is None
    assert result.errors is not None
    assert len(result.errors) == 1

    formatted = result.errors[0].formatted

    assert formatted["message"] == WORKSPACE_NOT_FOUND_MESSAGE
    assert formatted["extensions"] == {"code": "NOT_FOUND"}
    assert membership_service.slugs == ["nope"]


async def test_the_workspace_id_is_not_published_on_the_team_type():
    """An absence nobody asserts is indistinguishable from an omission.

    Publishing a tenant id invites accepting it back as an argument, which is
    exactly what CLAUDE.md forbids: the server must never trust a workspace id
    supplied by the frontend.
    """
    fields = set(schema.as_str().split("type Team {")[1].split("}")[0].split())

    assert "workspaceId:" not in fields
    assert "key:" in fields
