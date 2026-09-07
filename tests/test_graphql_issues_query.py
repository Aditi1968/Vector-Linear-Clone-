"""GraphQL transport tests for the issues connection. No database involved."""

from uuid import uuid4

from app.domain.errors import ValidationError, ValidationIssue
from app.domain.issues import (
    DEFAULT_ORDER,
    NO_FILTER,
    IssueFilter,
    IssueOrder,
    IssueOrderField,
    OrderDirection,
)
from app.domain.pagination import IssuePage, encode_issue_list_cursor
from app.domain.teams import WorkflowStateCategory
from app.graphql.schema import build_schema

from tests.conftest import (
    TEST_AUTHORIZED_SCOPE,
    TEST_WORKSPACE_SLUG,
    graphql_context,
    make_entity,
)


# Built directly rather than imported, so these tests need no DATABASE_URL.
schema = build_schema("test")


# Every document here names a workspace, because every workspace-scoped
# field now requires one. The slug is a variable rather than a literal so
# that the refusal tests can send a different one through the same document.
ISSUES_QUERY = """
query ListIssues($slug: String!, $first: Int!, $after: String) {
  issues(workspaceSlug: $slug, first: $first, after: $after) {
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
  issues(workspaceSlug: "acme") {
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


def Context(issue_service):
    """The real context, wired to one fake service.

    Built through the shared helper rather than as a bespoke object, because
    the resolvers now reach two more collaborators before the issue service:
    the auth service that says who is asking, and the membership service that
    says whether they may be in this workspace. A hand-rolled stand-in would
    have to grow both by hand and would drift from the one in conftest.
    """
    return graphql_context(issue_service=issue_service)


class FakeIssueService:
    def __init__(self, page: IssuePage):
        self._page = page
        self.calls: list[dict] = []
        self.counts: list[dict] = []

    async def list(
        self,
        *,
        scope,
        first: int,
        after: str | None,
        issue_filter=NO_FILTER,
        order=DEFAULT_ORDER,
    ):
        self.calls.append(
            {
                "scope": scope,
                "first": first,
                "after": after,
                "issue_filter": issue_filter,
                "order": order,
            }
        )

        return self._page

    async def count(self, *, scope, issue_filter=NO_FILTER):
        self.counts.append({"scope": scope, "issue_filter": issue_filter})

        return 7


class InvalidArgumentsService:
    def __init__(self, issues: list[ValidationIssue]):
        self._issues = issues

    async def list(self, *, scope, first: int, after: str | None, **kwargs):
        raise ValidationError(self._issues)


class BrokenIssueService:
    async def list(self, *, scope, first: int, after: str | None, **kwargs):
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
    assert service.calls == [
        {
            "scope": TEST_AUTHORIZED_SCOPE,
            "first": 50,
            "after": None,
            "issue_filter": NO_FILTER,
            "order": IssueOrder(),
        }
    ]


async def test_explicit_first_is_forwarded():
    service = FakeIssueService(empty_page())

    result = await schema.execute(
        ISSUES_QUERY,
        variable_values={"slug": TEST_WORKSPACE_SLUG, "first": 2, "after": None},
        context_value=Context(service),
    )

    assert result.errors is None
    assert service.calls == [
        {
            "scope": TEST_AUTHORIZED_SCOPE,
            "first": 2,
            "after": None,
            "issue_filter": NO_FILTER,
            "order": IssueOrder(),
        }
    ]


async def test_cursor_is_forwarded_unchanged():
    entity = make_entity(5)
    cursor = encode_issue_list_cursor(IssueOrder(), entity.created_at, entity.id)
    service = FakeIssueService(empty_page())

    result = await schema.execute(
        ISSUES_QUERY,
        variable_values={"slug": TEST_WORKSPACE_SLUG, "first": 10, "after": cursor},
        context_value=Context(service),
    )

    assert result.errors is None
    assert service.calls == [
        {
            "scope": TEST_AUTHORIZED_SCOPE,
            "first": 10,
            "after": cursor,
            "issue_filter": NO_FILTER,
            "order": IssueOrder(),
        }
    ]


async def test_domain_page_maps_onto_connection():
    nodes = [make_entity(2), make_entity(1)]
    page = IssuePage(nodes=nodes, has_next_page=True, end_cursor="CURSOR")

    result = await schema.execute(
        ISSUES_QUERY,
        variable_values={"slug": TEST_WORKSPACE_SLUG, "first": 2, "after": None},
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
        variable_values={"slug": TEST_WORKSPACE_SLUG, "first": 10, "after": None},
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
        variable_values={"slug": TEST_WORKSPACE_SLUG, "first": 10, "after": "bad"},
        context_value=Context(service),
    )

    assert result.errors is not None
    assert len(result.errors) == 1

    formatted = result.errors[0].formatted

    assert formatted["message"] == "Invalid issue list arguments"
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
        variable_values={"slug": TEST_WORKSPACE_SLUG, "first": 10, "after": None},
        context_value=Context(BrokenIssueService()),
    )

    assert result.errors is not None
    assert len(result.errors) == 1

    formatted = result.errors[0].formatted

    assert formatted["message"] != "Invalid issue list arguments"
    assert "extensions" not in formatted


# The filter and the ordering, as a client sends them. `assigneeId` is a
# variable so one document can send a uuid, an explicit null and nothing at
# all -- which is the whole point of the three-state input and the only way to
# test it through the transport that produces the distinction.
FILTERED_QUERY = """
query FilteredIssues(
  $slug: String!
  $filter: IssueFilterInput
  $orderBy: IssueOrderInput
) {
  issues(workspaceSlug: $slug, filter: $filter, orderBy: $orderBy, first: 10) {
    nodes {
      id
    }

    totalCount
  }
}
"""


async def test_every_filter_field_reaches_the_service():
    service = FakeIssueService(empty_page())
    team = uuid4()
    assignee = uuid4()
    state = uuid4()
    label = uuid4()
    project = uuid4()
    cycle = uuid4()

    result = await schema.execute(
        FILTERED_QUERY,
        variable_values={
            "slug": TEST_WORKSPACE_SLUG,
            "filter": {
                "teamId": str(team),
                "assigneeId": str(assignee),
                "workflowStateId": str(state),
                "stateCategory": "STARTED",
                "labelId": str(label),
                "priority": 1,
                "projectId": str(project),
                "cycleId": str(cycle),
            },
        },
        context_value=Context(service),
    )

    assert result.errors is None
    assert service.calls[0]["issue_filter"] == IssueFilter(
        team_id=team,
        assignee_id=assignee,
        workflow_state_id=state,
        state_category=WorkflowStateCategory.STARTED,
        label_id=label,
        priority=1,
        project_id=project,
        cycle_id=cycle,
    )


async def test_an_explicit_null_assignee_asks_for_the_unassigned():
    """The three-state input, exercised where the three states exist.

    An omitted `assigneeId` is UNSET and an explicit null is None, and the
    difference is the whole "My Issues" versus "Unassigned" distinction. It
    can only be produced through the transport: the domain object has both
    values, but only GraphQL can tell an absent field from a null one.
    """
    service = FakeIssueService(empty_page())

    result = await schema.execute(
        FILTERED_QUERY,
        variable_values={
            "slug": TEST_WORKSPACE_SLUG,
            "filter": {"assigneeId": None, "projectId": None, "cycleId": None},
        },
        context_value=Context(service),
    )

    assert result.errors is None
    assert service.calls[0]["issue_filter"] == IssueFilter(
        assignee_id=None,
        project_id=None,
        cycle_id=None,
    )


async def test_an_explicit_null_on_a_not_null_column_is_no_filter_at_all():
    """`teamId: null` cannot mean "issues with no team": there are none.

    So it reads as the absence of a filter, exactly as it does everywhere
    else in this schema, rather than as a request for the empty set.
    """
    service = FakeIssueService(empty_page())

    result = await schema.execute(
        FILTERED_QUERY,
        variable_values={
            "slug": TEST_WORKSPACE_SLUG,
            "filter": {"teamId": None, "priority": None, "workflowStateId": None},
        },
        context_value=Context(service),
    )

    assert result.errors is None
    assert service.calls[0]["issue_filter"] == NO_FILTER


async def test_order_by_reaches_the_service_and_defaults_by_half():
    """`{field: PRIORITY}` alone keeps the default direction."""
    service = FakeIssueService(empty_page())

    result = await schema.execute(
        FILTERED_QUERY,
        variable_values={
            "slug": TEST_WORKSPACE_SLUG,
            "orderBy": {"field": "PRIORITY"},
        },
        context_value=Context(service),
    )

    assert result.errors is None
    assert service.calls[0]["order"] == IssueOrder(
        field=IssueOrderField.PRIORITY,
        direction=OrderDirection.DESC,
    )


async def test_total_count_runs_the_same_filter_as_the_page():
    """The number a column header states counts what the page selected.

    A `totalCount` computed under a different filter than its own nodes is
    worse than no number: it is a wrong one, rendered with confidence.
    """
    service = FakeIssueService(empty_page())
    project = uuid4()

    result = await schema.execute(
        FILTERED_QUERY,
        variable_values={
            "slug": TEST_WORKSPACE_SLUG,
            "filter": {"projectId": str(project)},
        },
        context_value=Context(service),
    )

    assert result.errors is None
    assert result.data == {"issues": {"nodes": [], "totalCount": 7}}
    assert service.counts == [
        {
            "scope": TEST_AUTHORIZED_SCOPE,
            "issue_filter": IssueFilter(project_id=project),
        }
    ]


async def test_a_document_that_omits_total_count_never_counts():
    """The second aggregate is a resolver, so not selecting it does not run it."""
    service = FakeIssueService(empty_page())

    result = await schema.execute(
        ISSUES_QUERY,
        variable_values={"slug": TEST_WORKSPACE_SLUG, "first": 10, "after": None},
        context_value=Context(service),
    )

    assert result.errors is None
    assert service.counts == []
