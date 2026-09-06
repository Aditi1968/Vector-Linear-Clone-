"""GraphQL transport tests for the issueCreate payload contract.

These never touch Neon: the invalid case uses the real IssueService with a
pool that refuses to be acquired, and the success case uses a fake service.
"""

from uuid import UUID

from app.domain.issues import IssueEntity
from app.graphql.schema import build_schema
from app.repositories.issues import IssueRepository
from app.repositories.teams import TeamRepository
from app.services.issues import IssueService
from app.services.teams import TeamService

from tests.conftest import (
    TEST_AUTHORIZED_SCOPE,
    TEST_TEAM_ID,
    TEST_USER_ID,
    TEST_WORKSPACE_SLUG,
    ExplodingPool,
    graphql_context,
    make_entity,
)


# Built directly rather than imported, so these tests need no DATABASE_URL.
schema = build_schema("test")


# The two fields every create carries now. Spread into each case so that a
# test varies only the field it is about.
CREATE_INPUT = {
    "workspaceSlug": TEST_WORKSPACE_SLUG,
    "teamId": str(TEST_TEAM_ID),
}

ISSUE_CREATE_MUTATION = """
mutation CreateIssue($input: IssueCreateInput!) {
  issueCreate(input: $input) {
    issue {
      id
      title
      priority
    }

    errors {
      field
      code
      message
    }
  }
}
"""


def Context(issue_service):
    """The context these tests execute a document against.

    Through `graphql_context` rather than a local stand-in class, so that a
    slot added to `VectorContext` is filled in one place. The viewer and the
    membership come from that helper's defaults, and both are load-bearing
    now: `issueCreate` authorizes the workspace named on the input and then
    records authorship from the membership row that authorized it, so there
    is no anonymous create left to test.
    """
    return graphql_context(issue_service=issue_service)


class FakeIssueService:
    """Returns a fixed entity without any database access."""

    def __init__(self, entity: IssueEntity):
        self._entity = entity
        self.calls: list[dict] = []

    async def create(self, *, scope, team_id, **fields):
        self.calls.append({"scope": scope, "team_id": team_id, **fields})

        return self._entity


class BrokenIssueService:
    """Raises an unexpected failure, standing in for an asyncpg outage."""

    async def create(self, *, scope, team_id, **fields):
        raise RuntimeError("connection reset by peer")


async def test_invalid_input_returns_structured_payload():
    pool = ExplodingPool()
    context = Context(
        IssueService(
            pool=pool,
            repository=IssueRepository(),
            teams=TeamService(pool=pool, repository=TeamRepository()),
        )
    )

    result = await schema.execute(
        ISSUE_CREATE_MUTATION,
        variable_values={
            "input": CREATE_INPUT | {"title": "", "description": None, "priority": 99}
        },
        context_value=context,
    )

    assert result.errors is None
    assert result.data == {
        "issueCreate": {
            "issue": None,
            "errors": [
                {
                    "field": "title",
                    "code": "REQUIRED",
                    "message": "Title is required",
                },
                {
                    "field": "priority",
                    "code": "OUT_OF_RANGE",
                    "message": "Priority must be between 0 and 4",
                },
            ],
        }
    }

    # No connection acquired, therefore no INSERT.
    assert pool.acquire_count == 0


async def test_valid_input_returns_issue_and_empty_errors():
    entity = make_entity(
        1,
        id=UUID("00000000-0000-7000-8000-000000000001"),
        title="A valid title",
        description="described",
        priority=2,
    )
    service = FakeIssueService(entity)

    result = await schema.execute(
        ISSUE_CREATE_MUTATION,
        variable_values={
            "input": CREATE_INPUT
            | {
                "title": "A valid title",
                "description": "described",
                "priority": 2,
            }
        },
        context_value=Context(service),
    )

    assert result.errors is None
    assert result.data == {
        "issueCreate": {
            "issue": {
                "id": "00000000-0000-7000-8000-000000000001",
                "title": "A valid title",
                "priority": 2,
            },
            "errors": [],
        }
    }

    # The scope that reaches the service is the AUTHORIZED one -- built from
    # a membership row -- and not merely a workspace the document named. The
    # team is the document's, because the client says which team it is filing
    # against; the workspace is not, because a slug only selects what is being
    # asked about and the membership decides whether it may be.
    #
    # `creator_id` is the viewer's id and is asserted rather than ignored: the
    # resolver must take authorship from the session, so any other value here
    # would mean it came from somewhere a client can reach.
    assert service.calls == [
        {
            "scope": TEST_AUTHORIZED_SCOPE,
            "team_id": TEST_TEAM_ID,
            "title": "A valid title",
            "description": "described",
            "priority": 2,
            "assignee_id": None,
            "creator_id": TEST_USER_ID,
            "estimate": None,
            "due_date": None,
        }
    ]


async def test_unexpected_errors_are_not_converted_to_validation_errors():
    """A non-ValidationError must still surface as a top-level GraphQL error."""
    result = await schema.execute(
        ISSUE_CREATE_MUTATION,
        variable_values={
            "input": CREATE_INPUT
            | {"title": "A valid title", "description": None, "priority": 2}
        },
        context_value=Context(BrokenIssueService()),
    )

    assert result.errors is not None
    assert len(result.errors) == 1
    assert result.data is None
