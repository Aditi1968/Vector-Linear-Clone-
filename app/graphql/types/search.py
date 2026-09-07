import strawberry

from app.domain.search import SearchResults
from app.domain.semantic_search import DuplicateSuggestion
from app.domain.tenancy import WorkspaceScope
from app.graphql.types.issue import IssueType
from app.graphql.types.project import ProjectType


@strawberry.type(name="SearchResults")
class SearchResultsType:
    """What one search found, as two typed lists.

    A struct and not a `[SearchHit!]!` union. The union is the shape that
    looks right in SDL and is worse to hold: every client renders an issue and
    a project differently, so a heterogeneous list is sorted into these two
    on arrival, and each of them then has to spread two inline fragments to
    read fields it already knew the type of. The struct also lets one kind
    return nothing without that being indistinguishable from a shorter list.

    No `pageInfo`. See `app.domain.search.SearchResults` for why a relevance
    ordering is not a thing a cursor can resume.
    """

    issues: list[IssueType]
    projects: list[ProjectType]

    @classmethod
    def from_domain(
        cls, results: SearchResults, scope: WorkspaceScope
    ) -> "SearchResultsType":
        """Build the result types, each carrying the workspace it was found in.

        The scope is threaded through rather than left to a resolver to
        rediscover: `Issue.labels`, `Issue.parent` and `Project.milestones`
        resolve against the scope their parent object carries, so a hit built
        without one would resolve its nested fields against nothing.
        """
        return cls(
            issues=[IssueType.from_entity(entity, scope) for entity in results.issues],
            projects=[
                ProjectType.from_entity(entity, scope) for entity in results.projects
            ],
        )


@strawberry.type(name="DuplicateSuggestion")
class DuplicateSuggestionType:
    """One issue that might already be the one being written, and how close.

    A wrapper type rather than a `similarity` field on `Issue`, because the
    number is a property of the PAIR and not of the issue: the same issue is
    0.91 similar to one draft and 0.12 similar to the next, so a field on
    `Issue` would be a value that changed meaning depending on which query
    returned it -- and would be selectable, as null, from every other query
    that returns an issue.

    `similarity` is a similarity and never a certainty. See
    `app.domain.semantic_search.DuplicateSuggestion` for what that obliges a
    client to do with it -- present a candidate a person judges, never a
    verdict the product has reached.
    """

    issue: IssueType
    similarity: float

    @classmethod
    def from_domain(
        cls, suggestion: DuplicateSuggestion, scope: WorkspaceScope
    ) -> "DuplicateSuggestionType":
        return cls(
            issue=IssueType.from_entity(suggestion.issue, scope),
            similarity=suggestion.similarity,
        )
