"""What a GraphQL error is allowed to tell a client. No database involved.

An unexpected exception carries whatever its author put in the message --
a connection string, a failing SQL statement, the shape of a table -- and
GraphQL's default is to hand that straight to the caller. Masking flips the
default, so these tests guard both directions of the flip: internals must
not escape, and the errors the product deliberately publishes must still
arrive intact, including the ones that travel in `data` rather than in
`errors`.

The marker string is asserted against the whole serialized response rather
than against `errors[0].message`, because "leaked" means "reached the wire"
-- through extensions, through a path, through anywhere.
"""

import json
from unittest import mock

import pytest
from graphql import GraphQLError, ValidationRule

from app.config import Environment
from app.domain.errors import ValidationError, ValidationIssue
from app.domain.pagination import IssuePage
from app.graphql import limits
from app.graphql.schema import MASKED_ERROR_MESSAGE, build_schema

from tests.conftest import make_entity


ALL_ENVIRONMENTS: list[Environment] = ["development", "test", "production"]

# Distinctive enough that finding it anywhere in the response is proof of a
# leak and not a coincidence.
INTERNAL_MARKER = "asyncpg_password_hunter2_9f3c1d"

ISSUES_QUERY = """
query ListIssues {
  issues(first: 1) {
    nodes {
      id
      title
    }
  }
}
"""

# Every Issue scalar plus the connection's page info: at the default page
# size of 50 this costs 450 of the 1000 budget, so three of them break it.
FULL_PAGE_SELECTION = """
  nodes { id title description priority completedAt createdAt updatedAt }
  pageInfo { hasNextPage endCursor }
"""

ISSUE_CREATE_MUTATION = """
mutation CreateIssue($input: IssueCreateInput!) {
  issueCreate(input: $input) {
    issue {
      id
      title
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


class ExplodingIssueService:
    """Fails the way a driver or a bug does: an exception nobody expected."""

    async def list(self, *, first: int, after: str | None):
        raise RuntimeError(INTERNAL_MARKER)

    async def create(self, *, title, description, priority):
        raise RuntimeError(INTERNAL_MARKER)


class LeakyIssueService:
    """Raises a GraphQLError, but not one the product means to publish.

    Deliberately shaped like an internal handler that "helpfully" attached
    the failing statement: raising a GraphQLError is not by itself a
    decision to publish, so this must be masked exactly like a RuntimeError.
    """

    async def list(self, *, first: int, after: str | None):
        raise GraphQLError(
            INTERNAL_MARKER,
            extensions={"code": "DB_CONNECTION_FAILED", "statement": INTERNAL_MARKER},
        )


class RejectingIssueService:
    """Fails the way user input does: a domain ValidationError."""

    def __init__(self, issues: list[ValidationIssue]):
        self._issues = issues

    async def list(self, *, first: int, after: str | None):
        raise ValidationError(self._issues)

    async def create(self, *, title, description, priority):
        raise ValidationError(self._issues)


class FakeIssueService:
    def __init__(self, page: IssuePage):
        self._page = page

    async def list(self, *, first: int, after: str | None):
        return self._page


def serialize(result) -> str:
    """The response as the transport would put it on the wire.

    `GraphQLError.formatted` is the spec-shaped dict the HTTP layer sends,
    so serializing that is what proves nothing leaked -- reading
    `error.message` would miss a leak that rode along in extensions.
    """
    return json.dumps(
        {
            "data": result.data,
            "errors": [error.formatted for error in result.errors or []],
            "extensions": result.extensions,
        },
        default=str,
    )


@pytest.mark.parametrize("environment", ALL_ENVIRONMENTS)
async def test_unexpected_exceptions_are_masked(environment: Environment):
    """An exception nobody planned for must reach the client as nothing."""
    result = await build_schema(environment).execute(
        ISSUES_QUERY,
        context_value=Context(ExplodingIssueService()),
    )

    response = serialize(result)

    assert INTERNAL_MARKER not in response
    assert "RuntimeError" not in response
    assert "Traceback" not in response
    assert result.errors is not None
    assert [error.message for error in result.errors] == [MASKED_ERROR_MESSAGE]


async def test_an_error_code_outside_the_allowlist_is_still_masked():
    """A GraphQLError is not itself a passport, and neither is any code.

    The allowlist is the whole distinction: without it, "was raised as a
    GraphQLError" would be the test, and every library that raises one --
    or every handler that wraps a driver failure in one -- would be
    publishing on the product's behalf. The extensions have to go with the
    message, or a caller can still fingerprint the failure.
    """
    result = await build_schema("production").execute(
        ISSUES_QUERY,
        context_value=Context(LeakyIssueService()),
    )

    assert INTERNAL_MARKER not in serialize(result)
    assert result.errors is not None
    assert [error.message for error in result.errors] == [MASKED_ERROR_MESSAGE]
    assert "extensions" not in result.errors[0].formatted


@pytest.mark.parametrize("environment", ALL_ENVIRONMENTS)
async def test_public_input_errors_keep_their_code_and_payload(
    environment: Environment,
):
    """BAD_USER_INPUT is a published contract; masking must not flatten it."""
    service = RejectingIssueService(
        [
            ValidationIssue(
                field="first",
                code="OUT_OF_RANGE",
                message="first must be between 1 and 100",
            )
        ]
    )

    result = await build_schema(environment).execute(
        ISSUES_QUERY,
        context_value=Context(service),
    )

    assert result.errors is not None

    error = result.errors[0]

    assert error.message == "Invalid pagination arguments"
    assert error.extensions == {
        "code": "BAD_USER_INPUT",
        "issues": [
            {
                "field": "first",
                "code": "OUT_OF_RANGE",
                "message": "first must be between 1 and 100",
            }
        ],
    }


@pytest.mark.parametrize("environment", ALL_ENVIRONMENTS)
async def test_typed_validation_payloads_are_untouched(environment: Environment):
    """issueCreate reports validation in `data`, which masking never sees."""
    service = RejectingIssueService(
        [
            ValidationIssue(
                field="title",
                code="REQUIRED",
                message="Title is required",
            )
        ]
    )

    result = await build_schema(environment).execute(
        ISSUE_CREATE_MUTATION,
        variable_values={"input": {"title": "", "priority": 0}},
        context_value=Context(service),
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
                }
            ],
        }
    }


async def test_an_unexpected_mutation_failure_is_masked():
    """The mutation's typed-error path must not become a leak either.

    `issueCreate` catches ValidationError and nothing else, so a driver
    failure leaves through GraphQL's error array -- the same route the
    query takes, and it has to be masked on both.
    """
    result = await build_schema("production").execute(
        ISSUE_CREATE_MUTATION,
        variable_values={"input": {"title": "Valid", "priority": 0}},
        context_value=Context(ExplodingIssueService()),
    )

    assert INTERNAL_MARKER not in serialize(result)
    assert result.errors is not None
    assert [error.message for error in result.errors] == [MASKED_ERROR_MESSAGE]


async def test_an_exception_while_validating_is_masked():
    """The phase the extension does not cover, so the one that leaked.

    strawberry wraps parsing in `except Exception` but leaves validation
    bare, so a rule that raises escapes the operation context that
    `MaskErrors` runs inside, and its message is coerced straight onto the
    response. A deep fragment chain reaches this for real by exhausting the
    recursion limit; the rule is replaced here instead so the test asserts
    the phase rather than one machine's stack depth.

    The rule is swapped on the module rather than injected into the schema
    because `operation_limit_extensions` builds its rule list per request,
    which is what makes the substitution reach the real `build_schema`
    lineup instead of a rearranged copy of it.
    """
    marker = "validation_marker_pg_hba_conf_7b21"

    class ExplodingRule(ValidationRule):
        def __init__(self, context):
            raise RuntimeError(marker)

    with mock.patch.object(limits, "OperationLimitsRule", ExplodingRule):
        result = await build_schema("production").execute(
            ISSUES_QUERY,
            context_value=Context(
                FakeIssueService(
                    IssuePage(nodes=[], has_next_page=False, end_cursor=None)
                )
            ),
        )

    response = serialize(result)

    assert marker not in response
    assert "RuntimeError" not in response
    assert result.errors is not None
    assert [error.message for error in result.errors] == [MASKED_ERROR_MESSAGE]
    assert "extensions" not in result.errors[0].formatted


def fragment_chain(links: int) -> str:
    """F0 spreading F1 spreading F2 ... each once, ending in a real query.

    Nothing here is over any budget -- three fields, two levels deep -- so
    the document is refused, if at all, only for exhausting the interpreter
    stack while the walk follows the chain.
    """
    definitions = ["query Chain { ...F0 }"]

    for link in range(links):
        definitions.append(f"fragment F{link} on Query {{ ...F{link + 1} }}")

    definitions.append(f"fragment F{links} on Query {{ issues {{ nodes {{ id }} }} }}")

    return "\n".join(definitions)


@pytest.mark.parametrize("links", [500, 2000])
async def test_a_deep_fragment_chain_cannot_leak_the_interpreters_error(
    links: int,
):
    """The real path, not a substituted rule: a document that breaks the walk.

    A long enough chain exhausts the recursion limit inside `validate`, and
    strawberry coerces that straight onto the response with the exception's
    own text. The assertion is one-sided on purpose -- a machine with more
    stack may simply answer the query -- because what must never happen is
    the interpreter's message reaching the caller, whatever the stack does.
    """
    result = await build_schema("production").execute(
        fragment_chain(links),
        context_value=Context(
            FakeIssueService(IssuePage(nodes=[], has_next_page=False, end_cursor=None))
        ),
    )

    response = serialize(result)

    assert "recursion" not in response.lower()
    assert "RecursionError" not in response
    assert "Traceback" not in response

    for error in result.errors or []:
        assert error.message == MASKED_ERROR_MESSAGE


@pytest.mark.parametrize(
    "document, expected",
    [
        (
            "query D { issues(first: 1) { nodes { " + "id " * 2001 + "} } }",
            "Query selects 2003 fields; the maximum is 2000.",
        ),
        (
            "query A { "
            + " ".join(
                f"a{index}: issues(first: 1) {{ nodes {{ id }} }}"
                for index in range(16)
            )
            + " }",
            "Query uses 16 aliases; the maximum is 15.",
        ),
        (
            "query C { "
            + " ".join(
                f"{letter}: issues {{ {FULL_PAGE_SELECTION} }}" for letter in "abc"
            )
            + " }",
            "Query complexity is 1350; the maximum is 1000.",
        ),
    ],
    ids=["selections", "aliases", "complexity"],
)
async def test_limit_errors_reach_the_client_with_their_real_text(
    document: str,
    expected: str,
):
    """Masking the validation phase must not swallow the limit messages.

    These are the client's own document being described back to it, and a
    caller told only `Internal server error` has no way to learn it asked
    for too much. They stay public on exactly the branch that reads an
    error with no originating exception -- so a mask drawn one step wider
    would take all three with it.
    """
    result = await build_schema("production").execute(
        document,
        context_value=Context(
            FakeIssueService(IssuePage(nodes=[], has_next_page=False, end_cursor=None))
        ),
    )

    assert result.errors is not None
    assert expected in [error.message for error in result.errors]


async def test_clients_are_still_told_about_their_own_mistakes():
    """Parse and validation errors describe the document, not the server.

    Masking these would leave a client with a generic failure and no way to
    learn it misspelled a field, so they are public by construction: they
    have no originating exception to hide.
    """
    result = await build_schema("production").execute(
        "query Typo { issues(first: 1) { nodes { titel } } }",
        context_value=Context(
            FakeIssueService(
                IssuePage(nodes=[make_entity(1)], has_next_page=False, end_cursor=None)
            )
        ),
    )

    assert result.errors is not None
    assert "titel" in result.errors[0].message


async def test_successful_queries_are_unaffected():
    """Masking runs on every operation, including the ones that work."""
    entity = make_entity(1)
    page = IssuePage(nodes=[entity], has_next_page=False, end_cursor=None)

    result = await build_schema("production").execute(
        ISSUES_QUERY,
        context_value=Context(FakeIssueService(page)),
    )

    assert result.errors is None
    assert result.data == {
        "issues": {"nodes": [{"id": str(entity.id), "title": entity.title}]}
    }
