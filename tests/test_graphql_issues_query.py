"""GraphQL transport tests for the issues connection. No database involved."""

from app.domain.errors import ValidationError, ValidationIssue
from app.domain.pagination import IssuePage, encode_issue_cursor
from app.graphql.schema import build_schema

from tests.conftest import make_entity


# Built directly rather than imported, so these tests need no DATABASE_URL.
schema = build_schema("test")


ISSUES_QUERY = """
query ListIssues($first: Int!, $after: String) {
  issues(first: $first, after: $after) {
    nodes {
      id
      title
    }

    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""

DEFAULT_ARGS_QUERY = """
query {
  issues {
    nodes {
      id
      title
    }

    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""


class Context:
    def __init__(self, issue_service):
        self.issue_service = issue_service


class FakeIssueService:
    def __init__(self, page: IssuePage):
        self._page = page
        self.calls: list[dict] = []

    async def list(self, *, first: int, after: str | None):
        self.calls.append({"first": first, "after": after})

        return self._page


class InvalidArgumentsService:
    def __init__(self, issues: list[ValidationIssue]):
        self._issues = issues

    async def list(self, *, first: int, after: str | None):
        raise ValidationError(self._issues)


class BrokenIssueService:
    async def list(self, *, first: int, after: str | None):
        raise RuntimeError("connection reset by peer")


def empty_page() -> IssuePage:
    return IssuePage(nodes=[], has_next_page=False, end_cursor=None)


async def test_default_arguments_are_first_50_and_no_cursor():
    service = FakeIssueService(empty_page())

    result = await schema.execute(
        DEFAULT_ARGS_QUERY,
        context_value=Context(service),
    )

    assert result.errors is None
    assert service.calls == [{"first": 50, "after": None}]


async def test_explicit_first_is_forwarded():
    service = FakeIssueService(empty_page())

    result = await schema.execute(
        ISSUES_QUERY,
        variable_values={"first": 2, "after": None},
        context_value=Context(service),
    )

    assert result.errors is None
    assert service.calls == [{"first": 2, "after": None}]


async def test_cursor_is_forwarded_unchanged():
    entity = make_entity(5)
    cursor = encode_issue_cursor(entity.created_at, entity.id)
    service = FakeIssueService(empty_page())

    result = await schema.execute(
        ISSUES_QUERY,
        variable_values={"first": 10, "after": cursor},
        context_value=Context(service),
    )

    assert result.errors is None
    assert service.calls == [{"first": 10, "after": cursor}]


async def test_domain_page_maps_onto_connection():
    nodes = [make_entity(2), make_entity(1)]
    page = IssuePage(nodes=nodes, has_next_page=True, end_cursor="CURSOR")

    result = await schema.execute(
        ISSUES_QUERY,
        variable_values={"first": 2, "after": None},
        context_value=Context(FakeIssueService(page)),
    )

    assert result.errors is None
    assert result.data == {
        "issues": {
            "nodes": [
                {"id": str(nodes[0].id), "title": nodes[0].title},
                {"id": str(nodes[1].id), "title": nodes[1].title},
            ],
            "pageInfo": {"hasNextPage": True, "endCursor": "CURSOR"},
        }
    }


async def test_empty_page_maps_to_null_end_cursor():
    result = await schema.execute(
        ISSUES_QUERY,
        variable_values={"first": 10, "after": None},
        context_value=Context(FakeIssueService(empty_page())),
    )

    assert result.errors is None
    assert result.data == {
        "issues": {
            "nodes": [],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }
    }


async def test_validation_error_becomes_structured_graphql_error():
    service = InvalidArgumentsService(
        [
            ValidationIssue(
                field="after",
                code="INVALID_CURSOR",
                message="Cursor is invalid",
            )
        ]
    )

    result = await schema.execute(
        ISSUES_QUERY,
        variable_values={"first": 10, "after": "bad"},
        context_value=Context(service),
    )

    assert result.errors is not None
    assert len(result.errors) == 1

    formatted = result.errors[0].formatted

    assert formatted["message"] == "Invalid pagination arguments"
    assert formatted["extensions"] == {
        "code": "BAD_USER_INPUT",
        "issues": [
            {
                "field": "after",
                "code": "INVALID_CURSOR",
                "message": "Cursor is invalid",
            }
        ],
    }


async def test_unexpected_errors_still_propagate():
    """A non-ValidationError must not be dressed up as bad user input."""
    result = await schema.execute(
        ISSUES_QUERY,
        variable_values={"first": 10, "after": None},
        context_value=Context(BrokenIssueService()),
    )

    assert result.errors is not None
    assert len(result.errors) == 1

    formatted = result.errors[0].formatted

    assert formatted["message"] != "Invalid pagination arguments"
    assert "extensions" not in formatted
