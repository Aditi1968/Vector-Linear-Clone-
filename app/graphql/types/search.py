import strawberry

from app.domain.embedding_jobs import IndexingState
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


@strawberry.type(name="EmbeddingIndexingState")
class EmbeddingIndexingStateType:
    """How much of this workspace's semantic index exists, for the UI to say so.

    THE TYPE THAT MAKES "no duplicates found" AN HONEST SENTENCE. Without it,
    `issueDuplicateSuggestions` returning `[]` is unreadable: it means "nothing
    is similar" on an indexed workspace and "nothing has been indexed" on a
    fresh one, and a client has no way to tell which. With it, a client renders
    "index building -- 412 pending" for the second and only claims to have found
    no duplicates for the first.

    Four scalars and no object graph, deliberately. This is a status line, not a
    resource: it has no id, nothing links to it, and a client that could page
    through the pending issues would be asking a different question -- "which of
    my issues are unindexed" is `issues` with a filter, and building it into
    this type would make a progress indicator into a second issue list.

    The three counts partition the workspace's LIVE issues -- archived ones are
    in none of them, because nothing embeds an archived issue -- so
    `indexed + pending + failed` is the number this progress is out of, and a
    client can render a bar without asking for a total separately.
    """

    # Issues whose stored embedding matches the model AND the current text. What
    # semantic search can actually see -- not what has ever been written.
    indexed: int

    # Issues with no current embedding that the queue has not given up on. This
    # is the "N" in "index building -- N pending", and it counts an edited issue
    # too: its old vector describes text that no longer exists, so it is
    # genuinely not indexed until it has been embedded again.
    pending: int

    # Issues the queue has stopped retrying. Separate from `pending` because
    # they are the number that does not go down on its own, which is the one an
    # operator has to be told about rather than a progress bar.
    #
    # No reason is carried, and none will be: `embedding_jobs.last_error` is an
    # exception's own text, which is exactly the raw internal detail this
    # project's error rules keep out of responses.
    failed: int

    # Whether this deployment has an embedder wired at all.
    #
    # False means every count above is zero because there is no model to be
    # fresh or stale against, NOT because the workspace is empty -- so a client
    # must read this before rendering the counts, and should say "semantic
    # search is not enabled here" rather than "0 of 0 indexed".
    enabled: bool

    @classmethod
    def from_domain(cls, state: IndexingState) -> "EmbeddingIndexingStateType":
        """No scope threaded through, unlike every other type in this module.

        `SearchResultsType` and `DuplicateSuggestionType` carry one because they
        hold issues whose nested fields resolve against it. This holds four
        integers and resolves nothing, so a scope here would be a parameter that
        exists to look consistent.
        """
        return cls(
            indexed=state.indexed,
            pending=state.pending,
            failed=state.failed,
            enabled=state.enabled,
        )
