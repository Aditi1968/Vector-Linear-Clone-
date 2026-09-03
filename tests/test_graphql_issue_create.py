"""GraphQL transport tests for the issueCreate payload contract.

These never touch Neon: the invalid case uses the real IssueService with a
pool that refuses to be acquired, and the success case uses a fake service.
"""

from datetime import datetime, timezone
from uuid import UUID

from app.domain.issues import IssueEntity
from app.graphql.schema import build_schema
from app.repositories.issues import IssueRepository
from app.services.issues import IssueService

from tests.conftest import ExplodingPool


# Built directly rather than imported, so these tests need no DATABASE_URL.
schema = build_schema("test")


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


class Context:
    def __init__(self, issue_service):
        self.issue_service = issue_service


class FakeIssueService:
    """Returns a fixed entity without any database access."""

    def __init__(self, entity: IssueEntity):
        self._entity = entity
        self.calls: list[dict] = []

    async def create(self, *, title: str, description: str | None, priority: int):
        self.calls.append(
            {"title": title, "description": description, "priority": priority}
        )

        return self._entity


class BrokenIssueService:
    """Raises an unexpected failure, standing in for an asyncpg outage."""

    async def create(self, *, title: str, description: str | None, priority: int):
        raise RuntimeError("connection reset by peer")


async def test_invalid_input_returns_structured_payload():
    pool = ExplodingPool()
    context = Context(IssueService(pool=pool, repository=IssueRepository()))

    result = await schema.execute(
        ISSUE_CREATE_MUTATION,
        variable_values={"input": {"title": "", "description": None, "priority": 99}},
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
    entity = IssueEntity(
        id=UUID("00000000-0000-7000-8000-000000000001"),
        title="A valid title",
        description="described",
        priority=2,
        completed_at=None,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    service = FakeIssueService(entity)

    result = await schema.execute(
        ISSUE_CREATE_MUTATION,
        variable_values={
            "input": {
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

    assert service.calls == [
        {"title": "A valid title", "description": "described", "priority": 2}
    ]


async def test_unexpected_errors_are_not_converted_to_validation_errors():
    """A non-ValidationError must still surface as a top-level GraphQL error."""
    result = await schema.execute(
        ISSUE_CREATE_MUTATION,
        variable_values={
            "input": {"title": "A valid title", "description": None, "priority": 2}
        },
        context_value=Context(BrokenIssueService()),
    )

    assert result.errors is not None
    assert len(result.errors) == 1
    assert result.data is None
