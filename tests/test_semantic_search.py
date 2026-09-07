"""Semantic search without a database: the embedder, the wiring, the bounds.

tests/test_migration_025_db.py proves the SQL against a real PostgreSQL with
pgvector -- the tenant scoping above all. This file covers what is decided
before any statement is issued:

  * that the fallback embedder is deterministic, unit-length and 384-wide,
    because every claim the schema makes rests on those three;
  * that a missing model library is a fallback and never a startup failure;
  * that a service wired WITHOUT an embedder is the lexical search this
    project had before -- the degradation path, exercised rather than assumed;
  * that a service wired WITH one issues the hybrid statement, with the model
    it is actually using;
  * that the bounds are enforced before a connection is acquired.
"""

import math
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.domain.errors import ValidationError
from app.domain.semantic_search import (
    EMBEDDING_DIMENSIONS,
    FUSION_DEPTH,
    MIN_DUPLICATE_SIMILARITY,
    RRF_K,
    DuplicateSuggestion,
    EmbeddingSource,
    embedding_text,
)
from app.services.embeddings import (
    HASHING_EMBEDDER_NAME,
    HashingEmbedder,
    load_embedder,
    vector_literal,
)
from app.services.issues import TITLE_MAX_LENGTH as ISSUE_TITLE_MAX_LENGTH
from app.services.search import (
    FIRST_MAX,
    REFRESH_MAX,
    TITLE_MAX_LENGTH,
    SearchService,
)

from tests.conftest import TEST_SCOPE, ExplodingPool, FakePool, make_entity


ISSUE_ID = UUID("00000000-0000-7000-8000-0000000000b1")


# ------------------------------------------------------------- the embedder


def test_the_fallback_embeds_into_the_width_the_schema_declares():
    """`vector(384)` refuses anything else, so this is the column's contract."""
    [vector] = HashingEmbedder().embed(["Deploy pipeline is flaky"])

    assert len(vector) == EMBEDDING_DIMENSIONS


def test_the_fallback_returns_unit_vectors():
    """Everything downstream assumes it.

    Migration 025 argues for `<=>` partly on the grounds that `1 - distance`
    is a cosine in [-1, 1], and that is only true for normalised operands. A
    vector of any other length would still store, still compare and still
    produce a number -- just not the number the API says it is returning.
    """
    vectors = HashingEmbedder().embed(
        ["Deploy pipeline is flaky", "a", "", "     ", "Ω≈ç√"]
    )

    for vector in vectors:
        assert math.isclose(
            math.sqrt(sum(value * value for value in vector)), 1.0, rel_tol=1e-9
        )


def test_the_fallback_is_deterministic_across_calls():
    """`blake2b` and not `hash()`, whose string seed is randomised per process.

    A per-process coordinate system would mean a vector written by one worker
    compared against a query embedded by another -- silently, with a plausible
    number coming back. This test cannot see a second process, but it does
    catch the mistake of reaching for `hash()` here at all.
    """
    text = "Retries on the flaky deploy pipeline"

    assert HashingEmbedder().embed([text]) == HashingEmbedder().embed([text])


def test_the_fallback_scores_overlapping_text_above_unrelated_text():
    """The one useful property a hashing embedder has.

    It is not semantics -- the module says so at length -- but cosine over
    hashed tokens does tolerate word order, length and partial overlap, which
    is what makes it worth storing at all.
    """
    embedder = HashingEmbedder()
    query, near, far = embedder.embed(
        [
            "deploy pipeline is flaky",
            "the deploy pipeline keeps being flaky on retries",
            "rewrite the onboarding documentation",
        ]
    )

    def cosine(a, b):
        return sum(x * y for x, y in zip(a, b, strict=True))

    assert cosine(query, near) > cosine(query, far)


def test_a_shared_prefix_scores_below_an_exact_word():
    """The stem feature is emitted BESIDE the token, never instead of it."""
    embedder = HashingEmbedder()
    query, exact, stemmed = embedder.embed(["deployment", "deployment", "deployed"])

    def cosine(a, b):
        return sum(x * y for x, y in zip(a, b, strict=True))

    assert cosine(query, exact) > cosine(query, stemmed) > 0


def test_the_batch_answers_one_vector_per_text_in_order():
    embedder = HashingEmbedder()
    texts = ["one", "two", "three"]

    assert embedder.embed(texts) == [embedder.embed([text])[0] for text in texts]


def test_a_vector_literal_is_what_pgvector_parses():
    assert vector_literal([1.0, -0.5, 0.25]) == "[1.0,-0.5,0.25]"


def test_a_vector_literal_does_not_round_the_model_output():
    """`repr`, not a formatted string: this text is parsed back into the float.

    Rounding here would store a different vector from the one the embedder
    produced, which is a silent accuracy loss with nothing reporting it.
    """
    value = 0.1234567890123456

    assert str(value) in vector_literal([value])


# ----------------------------------------------------------- loading a model


def test_the_embedder_falls_back_when_no_model_library_is_installed(monkeypatch):
    """A missing optional dependency is a fallback and never a startup error.

    The whole point of the design: `pip install` of a 130MB model stack is not
    a precondition for this application booting.
    """
    load_embedder.cache_clear()
    monkeypatch.setattr(
        "app.services.embeddings.importlib.util.find_spec", lambda name: None
    )

    embedder = load_embedder()
    load_embedder.cache_clear()

    assert embedder.name == HASHING_EMBEDDER_NAME


def test_a_model_that_fails_to_load_falls_back_rather_than_raising(monkeypatch):
    """Installed but broken is the same answer as absent, deliberately.

    Weights that will not download, an incompatible checkpoint, a missing
    backend: none of them is a reason for the application to refuse to start.
    """
    load_embedder.cache_clear()
    monkeypatch.setattr(
        "app.services.embeddings.importlib.util.find_spec",
        lambda name: SimpleNamespace(),
    )

    def explode(name):
        raise OSError("no weights on this machine")

    monkeypatch.setattr("app.services.embeddings.importlib.import_module", explode)

    embedder = load_embedder()
    load_embedder.cache_clear()

    assert embedder.name == HASHING_EMBEDDER_NAME


def test_a_working_model_library_is_preferred_over_the_fallback(monkeypatch):
    """And its NAME is what reaches `issue_embeddings.model`.

    That name is the whole invalidation mechanism -- change embedders and
    every stored vector stops matching -- so a model whose vectors were filed
    under 'hashing-v1' would be compared against the fallback's coordinates.
    """
    load_embedder.cache_clear()

    class FakeModel:
        def __init__(self, name):
            self.name = name

        def encode(self, texts, **kwargs):
            return [[0.0] * (EMBEDDING_DIMENSIONS - 1) + [1.0] for _ in texts]

    monkeypatch.setattr(
        "app.services.embeddings.importlib.util.find_spec",
        lambda name: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "app.services.embeddings.importlib.import_module",
        lambda name: SimpleNamespace(SentenceTransformer=FakeModel),
    )

    embedder = load_embedder()
    load_embedder.cache_clear()

    assert embedder.name == "sentence-transformers/all-MiniLM-L6-v2"
    assert embedder.embed(["anything"]) == [[0.0] * (EMBEDDING_DIMENSIONS - 1) + [1.0]]


def test_the_embedder_is_built_once_per_process():
    """`get_context` runs per request; a model loaded per request is a disaster."""
    assert load_embedder() is load_embedder()


# ------------------------------------------------------------- the text rule


@pytest.mark.parametrize(
    ("title", "description", "expected"),
    [
        ("Deploy fails", "the build dies", "Deploy fails\n\nthe build dies"),
        ("Deploy fails", None, "Deploy fails"),
        # An empty description is the same as no description: joining a blank
        # line onto nothing would embed trailing whitespace as content.
        ("Deploy fails", "", "Deploy fails"),
    ],
)
def test_the_embedded_text_is_the_title_and_the_description(
    title, description, expected
):
    assert embedding_text(title, description) == expected


# ------------------------------------------------------- fakes for the service


class FakeIssueSearch:
    """Just enough IssueRepository for SearchService's lexical path."""

    def __init__(self, rows=None, exact=None):
        self.rows = rows if rows is not None else []
        self.exact = exact
        self.search_calls: list[dict] = []

    async def search(self, connection, *, scope, query, limit):
        self.search_calls.append({"scope": scope, "query": query, "limit": limit})

        return list(self.rows)

    async def get_by_identifier(self, connection, *, scope, team_key, number):
        return self.exact


class FakeProjectSearch:
    def __init__(self, rows=None):
        self.rows = rows if rows is not None else []

    async def search(self, connection, *, scope, query, limit):
        return list(self.rows)


class FakeEmbeddingRepository:
    """Records what the service asked for, and answers canned rows."""

    def __init__(self, hybrid=None, similar=None, stale=None, stored=True):
        self.hybrid = hybrid if hybrid is not None else []
        self.similar = similar if similar is not None else []
        self.stale = stale if stale is not None else []
        self.stored = stored
        self.hybrid_calls: list[dict] = []
        self.similar_calls: list[dict] = []
        self.stale_calls: list[dict] = []
        self.upsert_calls: list[dict] = []

    async def search_hybrid(self, connection, **kwargs):
        self.hybrid_calls.append(kwargs)

        return list(self.hybrid)

    async def search_similar(self, connection, **kwargs):
        self.similar_calls.append(kwargs)

        return list(self.similar)

    async def list_stale(self, connection, **kwargs):
        self.stale_calls.append(kwargs)

        return list(self.stale)

    async def upsert(self, connection, **kwargs):
        self.upsert_calls.append(kwargs)

        return self.stored


def service(*, pool=None, issues=None, projects=None, embeddings=None, embedder=None):
    return SearchService(
        pool=pool if pool is not None else FakePool(),
        issue_repository=issues if issues is not None else FakeIssueSearch(),
        project_repository=projects if projects is not None else FakeProjectSearch(),
        embedding_repository=embeddings,
        embedder=embedder,
    )


# ---------------------------------------------------- degrading to lexical


async def test_a_service_without_an_embedder_runs_the_lexical_search():
    """THE DEGRADATION PATH. Not a special case -- the original code path.

    A deployment with no `vector` extension and no model wires None here, and
    every search is byte for byte the statement migration 011 built. That is
    what makes "semantic search is optional" a fact about construction rather
    than a promise about error handling.
    """
    issues = FakeIssueSearch(rows=[make_entity(1)])
    embeddings = FakeEmbeddingRepository(hybrid=[make_entity(2)])

    results = await service(issues=issues, embeddings=embeddings).search(
        scope=TEST_SCOPE, query="deploy", first=10
    )

    assert [issue.id for issue in results.issues] == [make_entity(1).id]
    assert issues.search_calls
    assert embeddings.hybrid_calls == []


async def test_an_embedder_without_a_repository_also_stays_lexical():
    """Both or neither: an embedder with nothing to compare against is neither."""
    issues = FakeIssueSearch(rows=[make_entity(1)])

    results = await service(issues=issues, embedder=HashingEmbedder()).search(
        scope=TEST_SCOPE, query="deploy", first=10
    )

    assert [issue.id for issue in results.issues] == [make_entity(1).id]
    assert issues.search_calls


async def test_duplicate_suggestions_are_empty_without_an_embedder():
    """Empty, and deliberately not a lexical impostor.

    The fallback would have to report a `ts_rank` as a `similarity`, and a
    client rendering "82% similar" from a term-density score would be showing
    a number that means nothing it claims.
    """
    embeddings = FakeEmbeddingRepository(
        similar=[DuplicateSuggestion(issue=make_entity(1), similarity=0.9)]
    )

    suggestions = await service(embeddings=embeddings).suggest_duplicates(
        scope=TEST_SCOPE,
        title="Deploy pipeline is flaky",
        description=None,
        exclude_issue_id=None,
        first=5,
    )

    assert suggestions == []
    assert embeddings.similar_calls == []


async def test_a_refresh_without_an_embedder_writes_nothing():
    embeddings = FakeEmbeddingRepository(
        stale=[
            EmbeddingSource(
                issue_id=ISSUE_ID, source_digest="a" * 64, title="X", description=None
            )
        ]
    )

    written = await service(embeddings=embeddings).refresh_embeddings(
        scope=TEST_SCOPE, limit=10
    )

    assert written == 0
    assert embeddings.stale_calls == []


# ----------------------------------------------------------------- hybrid on


async def test_a_wired_service_issues_the_hybrid_statement():
    issues = FakeIssueSearch(rows=[make_entity(1)])
    embeddings = FakeEmbeddingRepository(hybrid=[make_entity(2)])

    results = await service(
        issues=issues, embeddings=embeddings, embedder=HashingEmbedder()
    ).search(scope=TEST_SCOPE, query="deploy", first=10)

    assert [issue.id for issue in results.issues] == [make_entity(2).id]
    assert issues.search_calls == []


async def test_the_hybrid_read_carries_the_scope_the_model_and_the_constants():
    """Every argument the SQL's correctness depends on, asserted at the call.

    The scope above all: it is what becomes `workspace_id = $1` inside BOTH
    arms of the fusion. The model name is what keeps two embedders' vectors
    from being compared. RRF_K and FUSION_DEPTH are the fusion itself.
    """
    embeddings = FakeEmbeddingRepository()
    embedder = HashingEmbedder()

    await service(embeddings=embeddings, embedder=embedder).search(
        scope=TEST_SCOPE, query="deploy", first=7
    )

    [call] = embeddings.hybrid_calls

    assert call["scope"] is TEST_SCOPE
    assert call["query"] == "deploy"
    assert call["model"] == embedder.name
    assert call["rrf_k"] == RRF_K
    assert call["depth"] == FUSION_DEPTH
    assert call["limit"] == 7
    assert call["embedding"] == vector_literal(embedder.embed(["deploy"])[0])


async def test_the_hybrid_arm_ranks_deeper_than_the_page():
    """Fusion can only re-rank what it was given.

    Drawing `first` candidates per arm would mean a document ranked 21st
    lexically and 1st semantically never reaching the fusion -- which is
    exactly the document a hybrid search exists to surface.
    """
    embeddings = FakeEmbeddingRepository()

    await service(embeddings=embeddings, embedder=HashingEmbedder()).search(
        scope=TEST_SCOPE, query="deploy", first=5
    )

    [call] = embeddings.hybrid_calls

    assert call["depth"] > call["limit"]


async def test_an_identifier_hit_still_leads_the_hybrid_results():
    """The exact lookup outranks the fusion, and is deduplicated against it."""
    exact = make_entity(4)
    issues = FakeIssueSearch(exact=exact)
    embeddings = FakeEmbeddingRepository(hybrid=[make_entity(2), exact])

    results = await service(
        issues=issues, embeddings=embeddings, embedder=HashingEmbedder()
    ).search(scope=TEST_SCOPE, query="ENG-4", first=10)

    assert [issue.id for issue in results.issues] == [exact.id, make_entity(2).id]


async def test_duplicate_suggestions_pass_the_floor_and_the_exclusion():
    suggestion = DuplicateSuggestion(issue=make_entity(1), similarity=0.87)
    embeddings = FakeEmbeddingRepository(similar=[suggestion])
    embedder = HashingEmbedder()

    suggestions = await service(
        embeddings=embeddings, embedder=embedder
    ).suggest_duplicates(
        scope=TEST_SCOPE,
        title="Deploy pipeline is flaky",
        description="It keeps retrying",
        exclude_issue_id=ISSUE_ID,
        first=5,
    )

    [call] = embeddings.similar_calls

    assert suggestions == [suggestion]
    assert call["scope"] is TEST_SCOPE
    assert call["exclude_issue_id"] == ISSUE_ID
    assert call["min_similarity"] == MIN_DUPLICATE_SIMILARITY
    assert call["model"] == embedder.name
    assert call["embedding"] == vector_literal(
        embedder.embed(["Deploy pipeline is flaky\n\nIt keeps retrying"])[0]
    )


# --------------------------------------------------------------- the refresh


async def test_a_refresh_embeds_the_queue_and_reports_what_it_wrote():
    sources = [
        EmbeddingSource(
            issue_id=UUID(int=index),
            source_digest=f"{index:064d}",
            title=f"Issue {index}",
            description=None,
        )
        for index in range(1, 4)
    ]
    embeddings = FakeEmbeddingRepository(stale=sources)
    embedder = HashingEmbedder()

    written = await service(
        embeddings=embeddings, embedder=embedder
    ).refresh_embeddings(scope=TEST_SCOPE, limit=50)

    assert written == 3
    assert [call["issue_id"] for call in embeddings.upsert_calls] == [
        source.issue_id for source in sources
    ]
    # The digest the sweep READ travels with the write, so the statement can
    # refuse a row whose text changed while the model was running.
    assert [call["source_digest"] for call in embeddings.upsert_calls] == [
        source.source_digest for source in sources
    ]
    assert embeddings.stale_calls == [
        {"scope": TEST_SCOPE, "model": embedder.name, "limit": 50}
    ]


async def test_a_refresh_does_not_count_a_row_the_statement_declined():
    """An issue edited mid-sweep declines its own write and stays queued."""
    sources = [
        EmbeddingSource(
            issue_id=ISSUE_ID, source_digest="b" * 64, title="X", description=None
        )
    ]
    embeddings = FakeEmbeddingRepository(stale=sources, stored=False)

    written = await service(
        embeddings=embeddings, embedder=HashingEmbedder()
    ).refresh_embeddings(scope=TEST_SCOPE, limit=50)

    assert written == 0


async def test_an_empty_queue_costs_no_second_acquisition():
    """The write connection is taken only when there is something to write."""
    pool = FakePool()
    embeddings = FakeEmbeddingRepository(stale=[])

    written = await service(
        pool=pool, embeddings=embeddings, embedder=HashingEmbedder()
    ).refresh_embeddings(scope=TEST_SCOPE, limit=50)

    assert written == 0
    assert pool.acquire_count == 1


# ------------------------------------------------------------- the bounds


@pytest.mark.parametrize(
    ("title", "description", "first", "field"),
    [
        ("", None, 5, "title"),
        ("   ", None, 5, "title"),
        ("x" * (TITLE_MAX_LENGTH + 1), None, 5, "title"),
        ("Deploy", "x" * 10001, 5, "description"),
        ("Deploy", None, 0, "first"),
        ("Deploy", None, FIRST_MAX + 1, "first"),
    ],
)
async def test_bad_duplicate_arguments_are_refused_before_a_connection(
    exploding_pool: ExplodingPool, title, description, first, field
):
    """Validation runs before `pool.acquire()`, so a bad request costs nothing.

    A blank title matters more than the rest: the embedder would happily hash
    whitespace into a unit vector and return whichever issues sat near it,
    which is a list of suggestions computed from no input at all.
    """
    with pytest.raises(ValidationError) as raised:
        await service(
            pool=exploding_pool,
            embeddings=FakeEmbeddingRepository(),
            embedder=HashingEmbedder(),
        ).suggest_duplicates(
            scope=TEST_SCOPE,
            title=title,
            description=description,
            exclude_issue_id=None,
            first=first,
        )

    assert [issue.field for issue in raised.value.issues] == [field]
    assert exploding_pool.acquire_count == 0


@pytest.mark.parametrize("limit", [0, -1, REFRESH_MAX + 1])
async def test_a_refresh_outside_its_bounds_is_refused_before_a_connection(
    exploding_pool: ExplodingPool, limit
):
    with pytest.raises(ValidationError) as raised:
        await service(
            pool=exploding_pool,
            embeddings=FakeEmbeddingRepository(),
            embedder=HashingEmbedder(),
        ).refresh_embeddings(scope=TEST_SCOPE, limit=limit)

    assert [issue.field for issue in raised.value.issues] == ["limit"]
    assert exploding_pool.acquire_count == 0


def test_the_duplicate_title_bound_matches_the_one_issues_enforce():
    """A title this endpoint accepts and `issueCreate` refuses is a trap.

    Someone typing a 600-character title would be shown duplicate suggestions
    for a title they are then not allowed to file.
    """
    assert TITLE_MAX_LENGTH == ISSUE_TITLE_MAX_LENGTH
