import strawberry

from app.domain.search import SearchResults
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
    def from_domain(cls, results: SearchResults) -> "SearchResultsType":
        return cls(
            issues=[IssueType.from_entity(entity) for entity in results.issues],
            projects=[ProjectType.from_entity(entity) for entity in results.projects],
        )
