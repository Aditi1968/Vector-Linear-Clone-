"""Search without a database: the parse, the validation, the transport.

tests/test_search_db.py is where relevance, tenancy and the index are proved
against a real PostgreSQL. This file covers the three things that are decided
before any statement is issued -- whether a string is an identifier, whether
the arguments are answerable at all, and whether an unauthorized caller gets
as far as the service.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.domain.errors import (
    ValidationError,
    ValidationIssue,
    WorkspaceAccessDeniedError,
)
from app.domain.projects import ProjectEntity
from app.domain.search import QUERY_MAX_LENGTH, SearchResults, parse_issue_identifier
from app.domain.tenancy import AuthorizedWorkspaceScope
from app.graphql.queries.memberships import WORKSPACE_NOT_FOUND_MESSAGE
from app.graphql.schema import build_schema
from app.graphql.viewer import UNAUTHENTICATED_MESSAGE
from app.services.search import FIRST_MAX, SearchService

from tests.conftest import TEST_SCOPE, ExplodingPool, FakePool, make_entity


schema = build_schema("test")

VIEWER_USER_ID = UUID("00000000-0000-7000-8000-0000000000e1")
WORKSPACE_ID = UUID("00000000-0000-7000-8000-000000000001")

CREATED_AT = datetime(2026, 3, 1, 9, 0, 0, tzinfo=timezone.utc)

SCOPE = AuthorizedWorkspaceScope(
    workspace_id=WORKSPACE_ID,
    user_id=VIEWER_USER_ID,
    role="member",
)

SEARCH_QUERY = """
query Search($slug: String!, $query: String!) {
  search(workspaceSlug: $slug, query: $query) {
    issues {
      id
      identifier
      title
    }

    projects {
      id
      name
    }
  }
}
"""


def make_project(index: int, **overrides) -> ProjectEntity:
    fields = {
        "id": UUID(int=1000 + index),
        "name": f"Project {index}",
        "description": None,
        "state": "planned",
        "health": None,
        "target_date": None,
        "lead_id": None,
        "team_ids": (),
        "created_at": CREATED_AT,
        "updated_at": CREATED_AT,
    }

    return ProjectEntity(**(fields | overrides))


# ------------------------------------------------------------- the identifier


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("ENG-42", ("ENG", 42)),
        # Lowercase and mixed case name the same issue. `teams_key_format`
        # admits one spelling, so normalising here cannot collide.
        ("eng-42", ("ENG", 42)),
        ("Eng-42", ("ENG", 42)),
        ("  ENG-42  ", ("ENG", 42)),
        ("A1B2C3D4E5-1", ("A1B2C3D4E5", 1)),
    ],
)
def test_an_identifier_parses_to_a_key_and_a_number(query, expected):
    assert parse_issue_identifier(query) == expected


@pytest.mark.parametrize(
    "query",
    [
        "",
        "   ",
        "deploy pipeline",
        # No hyphen, so nothing to split on.
        "ENG42",
        # Two hyphens: `teams_key_format` forbids one in a key, so this cannot
        # be an identifier however it is grouped.
        "ENG-42-1",
        # Eleven characters, one past what `teams_key_format` admits.
        "ABCDEFGHIJK-1",
        # Starts with a digit, which no team key does.
        "1NG-42",
        "ENG-",
        "ENG-x",
        # Nineteen digits: past BIGINT, so this is not a number `issues.number`
        # could hold and binding it would raise rather than miss.
        "ENG-" + "9" * 19,
    ],
)
def test_a_string_that_cannot_name_an_issue_is_not_an_identifier(query):
    assert parse_issue_identifier(query) is None


# -------------------------------------------------------------- the arguments


class FakeIssues:
    """Records what it was asked and answers with canned entities."""

    def __init__(self, ranked=(), exact=None):
        self._ranked = list(ranked)
        self._exact = exact
        self.search_calls: list[dict] = []
        self.identifier_calls: list[dict] = []

    async def search(self, connection, *, scope, query, limit):
        self.search_calls.append({"scope": scope, "query": query, "limit": limit})

        return list(self._ranked)

    async def get_by_identifier(self, connection, *, scope, team_key, number):
        self.identifier_calls.append(
            {"scope": scope, "team_key": team_key, "number": number}
        )

        return self._exact


class FakeProjects:
    def __init__(self, rows=()):
        self._rows = list(rows)
        self.search_calls: list[dict] = []

    async def search(self, connection, *, scope, query, limit):
        self.search_calls.append({"scope": scope, "query": query, "limit": limit})

        return list(self._rows)


def make_service(pool=None, issues=None, projects=None) -> SearchService:
    return SearchService(
        pool=pool if pool is not None else FakePool(),
        issue_repository=issues if issues is not None else FakeIssues(),
        project_repository=projects if projects is not None else FakeProjects(),
    )


@pytest.mark.parametrize("first", [0, -1, FIRST_MAX + 1])
async def test_an_unanswerable_page_size_is_refused_before_a_connection(first):
    """Refused, never clamped, and refused before the pool is touched."""
    pool = ExplodingPool()

    with pytest.raises(ValidationError) as raised:
        await make_service(pool=pool).search(scope=TEST_SCOPE, query="x", first=first)

    assert [issue.field for issue in raised.value.issues] == ["first"]
    assert raised.value.issues[0].code == "OUT_OF_RANGE"
    assert pool.acquire_count == 0


async def test_an_overlong_query_is_refused_rather_than_truncated():
    """A bound on the parse, reported as an error a client can act on.

    Truncating would answer a different question than the one asked and give
    the caller no way to tell.
    """
    pool = ExplodingPool()

    with pytest.raises(ValidationError) as raised:
        await make_service(pool=pool).search(
            scope=TEST_SCOPE,
            query="x" * (QUERY_MAX_LENGTH + 1),
            first=10,
        )

    assert [issue.field for issue in raised.value.issues] == ["query"]
    assert pool.acquire_count == 0


async def test_every_violation_is_reported_at_once():
    """Two bad arguments produce two issues, not the first one twice."""
    with pytest.raises(ValidationError) as raised:
        await make_service(pool=ExplodingPool()).search(
            scope=TEST_SCOPE,
            query="x" * (QUERY_MAX_LENGTH + 1),
            first=0,
        )

    assert {issue.field for issue in raised.value.issues} == {"query", "first"}


@pytest.mark.parametrize("query", ["", "   ", "\t\n"])
async def test_an_empty_query_is_empty_results_and_not_an_error(query):
    """Empty, and established without a round trip.

    A blank box is the state a search field spends most of its life in, so
    answering it costs nothing.
    """
    pool = ExplodingPool()

    results = await make_service(pool=pool).search(
        scope=TEST_SCOPE,
        query=query,
        first=10,
    )

    assert results == SearchResults(issues=[], projects=[])
    assert pool.acquire_count == 0


# ------------------------------------------------------------ the two lookups


async def test_a_punctuation_only_query_still_reaches_the_database():
    """`!!!` is a tsquery with no terms, not a malformed one.

    Refusing it here would mean this service decides what a search *looks*
    like, and `websearch_to_tsquery` already answers it correctly with
    nothing. The check that it comes back empty is in the db suite; what
    matters here is that it is not turned into a validation error.
    """
    issues = FakeIssues()
    service = make_service(issues=issues)

    results = await service.search(scope=TEST_SCOPE, query="!!! ???", first=10)

    assert results == SearchResults(issues=[], projects=[])
    assert issues.search_calls[0]["query"] == "!!! ???"


async def test_prose_never_makes_the_identifier_lookup():
    issues = FakeIssues(ranked=[make_entity(1)])

    await make_service(issues=issues).search(
        scope=TEST_SCOPE,
        query="deploy pipeline",
        first=10,
    )

    assert issues.identifier_calls == []


async def test_an_identifier_hit_leads_the_results_and_appears_once():
    """The exact match goes first, and the text search does not duplicate it.

    `ENG-42` tokenises, so the ranked search can legitimately return the same
    row -- which is why the merge is a dedupe rather than a concatenation.
    """
    exact = make_entity(42)
    issues = FakeIssues(ranked=[make_entity(7), exact], exact=exact)

    results = await make_service(issues=issues).search(
        scope=TEST_SCOPE,
        query="ENG-42",
        first=10,
    )

    assert [issue.id for issue in results.issues] == [exact.id, UUID(int=7)]
    assert issues.identifier_calls[0]["team_key"] == "ENG"
    assert issues.identifier_calls[0]["number"] == 42


async def test_the_identifier_hit_does_not_widen_the_requested_page():
    """`first` bounds the combined list, so the exact hit costs a slot."""
    exact = make_entity(42)
    issues = FakeIssues(ranked=[make_entity(index) for index in (1, 2)], exact=exact)

    results = await make_service(issues=issues).search(
        scope=TEST_SCOPE,
        query="ENG-42",
        first=2,
    )

    assert [issue.id for issue in results.issues] == [exact.id, UUID(int=1)]


async def test_the_scope_reaches_both_repositories_unchanged():
    """Neither repository is given a workspace this service invented."""
    issues = FakeIssues()
    projects = FakeProjects()

    await make_service(issues=issues, projects=projects).search(
        scope=SCOPE,
        query="deploy",
        first=5,
    )

    assert issues.search_calls[0]["scope"] is SCOPE
    assert projects.search_calls[0]["scope"] is SCOPE


# ------------------------------------------------------------- the transport


class FakeMembershipService:
    def __init__(self, scope=SCOPE, deny=False):
        self._scope = scope
        self._deny = deny
        self.calls: list[dict] = []

    async def authorized_scope_for_slug(self, *, slug, user_id):
        self.calls.append({"slug": slug, "user_id": user_id})

        if self._deny:
            raise WorkspaceAccessDeniedError()

        return self._scope


class FakeSearchService:
    def __init__(self, results=None):
        self._results = results or SearchResults(issues=[], projects=[])
        self.calls: list[dict] = []

    async def search(self, *, scope, query, first):
        self.calls.append({"scope": scope, "query": query, "first": first})

        return self._results


class ExplodingSearchService:
    async def search(self, *, scope, query, first):
        raise AssertionError("an unauthorized caller reached the search service")


class Context:
    """Stands in for VectorContext. Mirrors tests/test_graphql_memberships.py."""

    def __init__(self, membership_service=None, search_service=None, viewer=None):
        self.membership_service = membership_service
        self.search_service = search_service
        self._viewer_user_id = viewer

    async def viewer(self):
        if self._viewer_user_id is None:
            return None

        return SimpleNamespace(id=self._viewer_user_id)


async def execute(context, *, slug="vector", query="deploy"):
    return await schema.execute(
        SEARCH_QUERY,
        variable_values={"slug": slug, "query": query},
        context_value=context,
    )


async def test_an_unauthenticated_caller_never_reaches_the_search():
    """Refused before the workspace is resolved, let alone read."""
    result = await execute(
        Context(
            membership_service=ExplodingSearchService(),
            search_service=ExplodingSearchService(),
            viewer=None,
        )
    )

    assert result.data is None
    assert [error.message for error in result.errors] == [UNAUTHENTICATED_MESSAGE]
    assert result.errors[0].extensions["code"] == "UNAUTHENTICATED"


async def test_a_workspace_the_viewer_is_not_in_reads_as_not_found():
    """The same message and code `myWorkspace` gives, imported not retyped.

    A different message here would let a client tell a workspace that exists
    from one that does not, which is the leak the shared constant closes.
    """
    memberships = FakeMembershipService(deny=True)

    result = await execute(
        Context(
            membership_service=memberships,
            search_service=ExplodingSearchService(),
            viewer=VIEWER_USER_ID,
        ),
        slug="someone-elses",
    )

    assert result.data is None
    assert [error.message for error in result.errors] == [WORKSPACE_NOT_FOUND_MESSAGE]
    assert result.errors[0].extensions["code"] == "NOT_FOUND"
    assert memberships.calls == [{"slug": "someone-elses", "user_id": VIEWER_USER_ID}]


async def test_the_search_is_scoped_by_the_authorized_workspace_not_the_slug():
    """The service receives the scope the membership lookup produced.

    The slug reaches exactly one frame -- the authorization call -- and the
    workspace id that reaches the search came out of `workspace_members`.
    """
    search = FakeSearchService()

    result = await execute(
        Context(
            membership_service=FakeMembershipService(),
            search_service=search,
            viewer=VIEWER_USER_ID,
        )
    )

    assert result.errors is None
    assert search.calls == [{"scope": SCOPE, "query": "deploy", "first": 20}]


async def test_both_kinds_of_result_cross_the_boundary():
    search = FakeSearchService(
        SearchResults(
            issues=[make_entity(42, title="Deploy pipeline")],
            projects=[make_project(1, name="Deploy")],
        )
    )

    result = await execute(
        Context(
            membership_service=FakeMembershipService(),
            search_service=search,
            viewer=VIEWER_USER_ID,
        )
    )

    assert result.errors is None
    assert result.data["search"] == {
        "issues": [
            {
                "id": str(UUID(int=42)),
                "identifier": "ENG-42",
                "title": "Deploy pipeline",
            }
        ],
        "projects": [{"id": str(UUID(int=1001)), "name": "Deploy"}],
    }


async def test_an_unanswerable_page_size_reaches_the_client_as_bad_user_input():
    """The service's structured issues, published under a public code."""

    class RefusingSearchService:
        async def search(self, *, scope, query, first):
            raise ValidationError(
                [
                    ValidationIssue(
                        field="first",
                        code="OUT_OF_RANGE",
                        message="first must be between 1 and 100",
                    )
                ]
            )

    result = await execute(
        Context(
            membership_service=FakeMembershipService(),
            search_service=RefusingSearchService(),
            viewer=VIEWER_USER_ID,
        )
    )

    assert result.data is None
    assert result.errors[0].extensions["code"] == "BAD_USER_INPUT"
    assert result.errors[0].extensions["issues"] == [
        {
            "field": "first",
            "code": "OUT_OF_RANGE",
            "message": "first must be between 1 and 100",
        }
    ]
