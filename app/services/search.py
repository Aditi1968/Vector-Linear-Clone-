from uuid import UUID

import asyncpg

from app.domain.embedding_jobs import IndexingState
from app.domain.errors import ValidationError, ValidationIssue
from app.domain.search import (
    QUERY_MAX_LENGTH,
    SearchResults,
    parse_issue_identifier,
)
from app.domain.semantic_search import (
    FUSION_DEPTH,
    MIN_DUPLICATE_SIMILARITY,
    RRF_K,
    DuplicateSuggestion,
    embedding_text,
)
from app.domain.tenancy import WorkspaceScope
from app.repositories.embedding_jobs import EmbeddingJobRepository
from app.repositories.embeddings import EmbeddingRepository
from app.repositories.issues import IssueRepository
from app.repositories.projects import ProjectRepository
from app.services.embeddings import Embedder, vector_literal


# The same bounds every other paginated read in this application states, and
# for the reason ProjectService._validate_list gives: two endpoints that
# disagreed about the legal page size would be a contract a client has to
# learn twice.
FIRST_MIN = 1
FIRST_MAX = 100

# The most issues one refresh sweep may embed. Restated from FIRST_MAX rather
# than shared with it, because it bounds a different thing: not a page a client
# renders, but how much model time one call may spend. A sweep is meant to be
# called repeatedly until it reports zero, so a small ceiling costs round trips
# and a large one costs a request that runs for minutes.
REFRESH_MAX = 500

# The longest title a duplicate check will consider, matching
# `app.services.issues.TITLE_MAX_LENGTH`. Restated rather than imported to
# avoid a service-to-service import for one integer; the two are asserted equal
# in tests/test_semantic_search.py, so a change to either fails there rather
# than silently letting this endpoint accept a title `issueCreate` would refuse.
TITLE_MAX_LENGTH = 500

# And the description bound, which exists here and not in IssueService because
# this endpoint embeds the text rather than storing it: an unbounded string
# would be an unbounded tokenise-and-hash on a public field.
DESCRIPTION_MAX_LENGTH = 10000


class SearchService:
    """Free-text search across a workspace's issues and projects.

    Two repositories rather than one, because the SQL for a table belongs to
    the repository that owns that table -- the same reason ProjectService
    reaches into IssueRepository to detach issues from a project it is
    deleting. A `SearchRepository` writing statements against `issues` and
    `projects` would be a third place either table's tenancy predicate has to
    be got right. `EmbeddingRepository` joins the third.

    Every read here is a single SELECT, so this acquires a connection without
    opening a transaction. The searches share one connection rather than
    taking one each: a search is one user action, and two acquisitions would
    let a busy pool serve half of it now and half of it later.

    The scope is an argument and is never derived here. This class cannot tell
    an authorized scope from a bare one -- see AuthorizedWorkspaceScope -- so
    the boundary that can is the only thing that supplies it.

    Semantic search is optional, and that is a construction-time fact
    -----------------------------------------------------------------
    `embeddings` and `embedder` may both be None, and then this class behaves
    exactly as it did before either existed: `search` is the lexical search
    migration 011 built, and `suggest_duplicates` answers with nothing.

    That is the degradation path, and it is a wire rather than a try/except.
    Deciding per request -- catching an UndefinedTableError, probing for the
    extension -- would mean the answer to "does this deployment have semantic
    search" varied with what the last query happened to hit, and it would
    swallow a genuine schema fault as a feature being off. Deciding once, at
    composition, means a deployment without the `vector` extension or without a
    model wires None and every read is honest about what it is.

    Both or neither. A repository with no embedder cannot embed the query it
    would search with, and an embedder with no repository has nothing to
    compare against, so the methods below check both and fall back if either is
    missing.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        issue_repository: IssueRepository,
        project_repository: ProjectRepository,
        embedding_repository: EmbeddingRepository | None = None,
        embedder: Embedder | None = None,
        job_repository: EmbeddingJobRepository | None = None,
    ):
        self._pool = pool
        self._issues = issue_repository
        self._projects = project_repository
        self._embeddings = embedding_repository
        self._embedder = embedder
        self._jobs = job_repository

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

        The issue arm is HYBRID when this service was wired with an embedder:
        the lexical ordering and the vector ordering fused by reciprocal rank,
        in one statement -- see `EmbeddingRepository.search_hybrid`. Without
        one it is the lexical search alone, which is the same rows in the same
        order this method has always returned.

        The PROJECT arm stays lexical either way. Migration 025 stores no
        project embeddings, and it says why: a project is found by its name,
        and a table nothing writes would be a table to keep fresh for nothing.

        The query is embedded BEFORE the connection is acquired. A real model
        is tens of milliseconds of CPU, and holding a pool slot across it would
        make every search's cost include a resource other requests are queued
        for.
        """
        self._validate(query=query, first=first)

        if not query.strip():
            return SearchResults(issues=[], projects=[])

        identifier = parse_issue_identifier(query)

        embeddings = self._embeddings
        embedder = self._embedder
        embedded = (
            vector_literal(embedder.embed([query])[0])
            if embeddings is not None and embedder is not None
            else None
        )

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

            if embeddings is not None and embedder is not None and embedded is not None:
                ranked = await embeddings.search_hybrid(
                    connection,
                    scope=scope,
                    query=query,
                    embedding=embedded,
                    model=embedder.name,
                    depth=FUSION_DEPTH,
                    rrf_k=RRF_K,
                    limit=first,
                )
            else:
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

    async def suggest_duplicates(
        self,
        *,
        scope: WorkspaceScope,
        title: str,
        description: str | None,
        exclude_issue_id: UUID | None,
        first: int,
    ) -> list[DuplicateSuggestion]:
        """Issues in this workspace that might already be the one being written.

        Takes the TEXT rather than an issue id, so the same call serves both
        moments it is wanted: before an issue exists, while somebody is typing
        it, and after, while somebody is editing it. `exclude_issue_id` is what
        distinguishes the two -- passed when editing, so an issue is never
        offered as a duplicate of itself; passed as None when creating, when
        there is nothing to exclude.

        Empty results when this service has no embedder, and that is the
        honest answer rather than a degraded one. The lexical fallback is
        available -- it would be `IssueRepository.search` over the title -- and
        is deliberately not used, because it would have to report a `ts_rank`
        as a `similarity`, and those are not the same quantity: a client
        rendering "82% similar" from a term-density score would be showing a
        number that means nothing it says. A caller that wants lexical matches
        for a title can call `search`.

        The floor is MIN_DUPLICATE_SIMILARITY and is not a parameter; see the
        constant for why a client may not lower it.
        """
        self._validate_duplicates(
            title=title,
            description=description,
            first=first,
        )

        embeddings = self._embeddings
        embedder = self._embedder

        if embeddings is None or embedder is None:
            return []

        embedded = vector_literal(
            embedder.embed([embedding_text(title, description)])[0]
        )

        async with self._pool.acquire() as connection:
            return await embeddings.search_similar(
                connection,
                scope=scope,
                embedding=embedded,
                model=embedder.name,
                exclude_issue_id=exclude_issue_id,
                min_similarity=MIN_DUPLICATE_SIMILARITY,
                limit=first,
            )

    async def refresh_embeddings(
        self,
        *,
        scope: WorkspaceScope,
        limit: int,
    ) -> int:
        """Embed up to `limit` of this workspace's issues that need it.

        Draining the regeneration queue, which is a query rather than a table
        -- see `EmbeddingRepository.list_stale`. Returns how many rows were
        actually written, so a caller loops until it reports zero.

        THIS IS THE ONLY THING THAT WRITES EMBEDDINGS, and doing it here rather
        than inside `IssueService.create` is a decision. Embedding on the write
        path would put model latency on every issue filed, make `issueCreate`
        fail when a model is broken, and couple the busiest transaction in the
        product to an optional dependency. It would also be wrong for the
        common case: an issue is edited three times in the minute after it is
        filed, and a synchronous embedder would compute four vectors to keep
        the last.

        The cost of that choice, stated: an issue is not semantically findable
        until a sweep has run. It is lexically findable immediately -- 011's
        generated column is written by the same statement that writes the issue
        -- so the gap degrades relevance rather than hiding the issue, which is
        the trade a background pass has to be worth.

        THREE PHASES, and the connection is deliberately released between them.
        Read the queue; run the model with no connection held; write. A real
        model is hundreds of milliseconds for a batch, and holding a pool slot
        across it would let one sweep starve the request path.

        The write is one transaction over the batch. Each row's own guard is in
        the statement -- an issue edited mid-sweep declines the write, see
        `EmbeddingRepository.upsert` -- so the transaction is not what makes
        the batch correct; it is what stops a failure at row 300 leaving 299
        embeddings written under a sweep that reported nothing.
        """
        self._validate_refresh(limit=limit)

        embeddings = self._embeddings
        embedder = self._embedder

        if embeddings is None or embedder is None:
            return 0

        async with self._pool.acquire() as connection:
            sources = await embeddings.list_stale(
                connection,
                scope=scope,
                model=embedder.name,
                limit=limit,
            )

        if not sources:
            return 0

        vectors = embedder.embed([source.text for source in sources])
        written = 0

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                # `strict=True`: an embedder returning a different number of
                # vectors than it was given texts is a bug in the embedder, and
                # zip would otherwise silently truncate to the shorter -- which
                # presents as a sweep that quietly stops embedding rows.
                for source, vector in zip(sources, vectors, strict=True):
                    stored = await embeddings.upsert(
                        connection,
                        scope=scope,
                        issue_id=source.issue_id,
                        model=embedder.name,
                        source_digest=source.source_digest,
                        embedding=vector_literal(vector),
                    )

                    if stored:
                        written += 1

        return written

    async def indexing_state(self, *, scope: WorkspaceScope) -> IndexingState:
        """How much of this workspace's semantic index actually exists.

        THE FIELD THAT LETS A CLIENT STOP GUESSING. `suggest_duplicates` answers
        with an empty list both when nothing is similar and when nothing is
        indexed, and on a fresh install it is always the second -- so every
        interface built on it has been rendering "no possible duplicates" over a
        workspace that has never been embedded. This is the read that says which
        of the two it is, so the answer can be "index building, N pending".

        It is the ONE place in the search feature that reports whether this
        deployment has an embedder at all, and that is a deliberate departure
        from what `suggest_duplicates` and `refresh_embeddings` do. Those two
        keep "no model" and "nothing found" indistinguishable, because for them
        a client could only use the difference to read the server's
        configuration off a result. Here the difference IS the question: a UI
        that cannot tell "still building" from "this deployment does not do
        this" has to guess, and it guesses wrong on exactly the fresh installs
        where getting it right matters. The caller is an authorized member of
        the workspace either way.

        Disabled -- and all three counts zero -- when either the embedder or the
        job repository is missing, without a round trip. That is honest rather
        than evasive: freshness is defined against the model doing the asking,
        so with no model there is no question to answer, and a count of zero
        beside `enabled: false` cannot be mistaken for a fully indexed
        workspace.

        No `first`, no bounds, nothing to validate: the arguments are a scope
        the caller has already been authorized for and a model name that came
        from this process's own configuration.
        """
        jobs = self._jobs
        embedder = self._embedder

        if jobs is None or embedder is None:
            return IndexingState(indexed=0, pending=0, failed=0, enabled=False)

        async with self._pool.acquire() as connection:
            return await jobs.indexing_state(
                connection,
                scope=scope,
                model=embedder.name,
            )

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

    @staticmethod
    def _validate_duplicates(
        *,
        title: str,
        description: str | None,
        first: int,
    ) -> None:
        """The bounds a duplicate check needs, collected and raised once.

        A blank title is refused rather than answered with nothing. The
        embedder would happily hash whitespace into a unit vector and return
        whichever issues happened to sit near it, which is a list of
        suggestions computed from no input -- worse than an error, because it
        looks like an answer.
        """
        issues: list[ValidationIssue] = []

        if not title.strip():
            issues.append(
                ValidationIssue(
                    field="title",
                    code="REQUIRED",
                    message="title must not be blank",
                )
            )

        if len(title) > TITLE_MAX_LENGTH:
            issues.append(
                ValidationIssue(
                    field="title",
                    code="TOO_LONG",
                    message=f"title must be at most {TITLE_MAX_LENGTH} characters",
                )
            )

        if description is not None and len(description) > DESCRIPTION_MAX_LENGTH:
            issues.append(
                ValidationIssue(
                    field="description",
                    code="TOO_LONG",
                    message=(
                        "description must be at most "
                        f"{DESCRIPTION_MAX_LENGTH} characters"
                    ),
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

    @staticmethod
    def _validate_refresh(*, limit: int) -> None:
        if limit < FIRST_MIN or limit > REFRESH_MAX:
            raise ValidationError(
                [
                    ValidationIssue(
                        field="limit",
                        code="OUT_OF_RANGE",
                        message=f"limit must be between {FIRST_MIN} and {REFRESH_MAX}",
                    )
                ]
            )
