import asyncpg

from app.domain.errors import ValidationError, ValidationIssue
from app.domain.search import (
    QUERY_MAX_LENGTH,
    SearchResults,
    parse_issue_identifier,
)
from app.domain.tenancy import WorkspaceScope
from app.repositories.issues import IssueRepository
from app.repositories.projects import ProjectRepository


# The same bounds every other paginated read in this application states, and
# for the reason ProjectService._validate_list gives: two endpoints that
# disagreed about the legal page size would be a contract a client has to
# learn twice.
FIRST_MIN = 1
FIRST_MAX = 100


class SearchService:
    """Free-text search across a workspace's issues and projects.

    Two repositories rather than one, because the SQL for a table belongs to
    the repository that owns that table -- the same reason ProjectService
    reaches into IssueRepository to detach issues from a project it is
    deleting. A `SearchRepository` writing statements against `issues` and
    `projects` would be a third place either table's tenancy predicate has to
    be got right.

    Every read here is a single SELECT, so this acquires a connection without
    opening a transaction. The two searches share one connection rather than
    taking one each: a search is one user action, and two acquisitions would
    let a busy pool serve half of it now and half of it later.

    The scope is an argument and is never derived here. This class cannot tell
    an authorized scope from a bare one -- see AuthorizedWorkspaceScope -- so
    the boundary that can is the only thing that supplies it.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        issue_repository: IssueRepository,
        project_repository: ProjectRepository,
    ):
        self._pool = pool
        self._issues = issue_repository
        self._projects = project_repository

    async def search(
        self,
        *,
        scope: WorkspaceScope,
        query: str,
        first: int,
    ) -> SearchResults:
        """What this workspace holds that matches `query`, best first.

        An empty or whitespace-only query returns empty results without
        touching the database. That is a short circuit and not a special case:
        `websearch_to_tsquery` would parse it to an empty tsquery, which `@@`
        answers false for every row, so the answer is the same either way --
        this only declines to spend a round trip establishing it. A
        punctuation-only query is left to the server for the same reason it is
        safe to: it is not an error there, it is a tsquery with no terms.

        `first` is validated and never silently clamped, so a client asking
        for 500 results is told the limit rather than quietly given 100 and
        left to conclude the workspace holds no more.

        The identifier path runs in addition to the text search, not instead
        of it. Someone typing `ENG-42` almost certainly wants that issue, and
        it goes first; but the string may also appear in the title of the
        issue that duplicates it, and dropping the text search would hide
        that. `first` bounds the combined list, so an identifier hit costs one
        of the requested slots rather than adding to them.
        """
        self._validate(query=query, first=first)

        if not query.strip():
            return SearchResults(issues=[], projects=[])

        identifier = parse_issue_identifier(query)

        async with self._pool.acquire() as connection:
            exact = None

            if identifier is not None:
                team_key, number = identifier
                exact = await self._issues.get_by_identifier(
                    connection,
                    scope=scope,
                    team_key=team_key,
                    number=number,
                )

            ranked = await self._issues.search(
                connection,
                scope=scope,
                query=query,
                limit=first,
            )

            projects = await self._projects.search(
                connection,
                scope=scope,
                query=query,
                limit=first,
            )

        if exact is not None:
            # Deduplicated by id, because the text search can legitimately have
            # found the same issue -- `ENG-42` in a title tokenises -- and the
            # same row appearing twice in one result list is a rendering bug
            # the client would have to fix instead.
            ranked = [exact] + [issue for issue in ranked if issue.id != exact.id]

        return SearchResults(issues=ranked[:first], projects=projects)

    @staticmethod
    def _validate(*, query: str, first: int) -> None:
        """Collect every violation, then raise once.

        The query length is checked and the query's *content* is not. There is
        no such thing as a malformed search: `websearch_to_tsquery` accepts
        anything a person can type, so rejecting a string here for looking odd
        would refuse a search the database would have answered. The length is
        different -- it bounds the parse, which is work a public field performs
        on an argument the caller chooses.
        """
        issues: list[ValidationIssue] = []

        if len(query) > QUERY_MAX_LENGTH:
            issues.append(
                ValidationIssue(
                    field="query",
                    code="TOO_LONG",
                    message=f"query must be at most {QUERY_MAX_LENGTH} characters",
                )
            )

        if first < FIRST_MIN or first > FIRST_MAX:
            issues.append(
                ValidationIssue(
                    field="first",
                    code="OUT_OF_RANGE",
                    message=f"first must be between {FIRST_MIN} and {FIRST_MAX}",
                )
            )

        if issues:
            raise ValidationError(issues)
