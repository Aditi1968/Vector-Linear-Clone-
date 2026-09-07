"""Migration 025 and hybrid search against a real PostgreSQL with pgvector.

tests/test_semantic_search.py proves what is decided before a statement is
issued. This file is about the statements and the schema, and there is one
claim here that matters more than all the others:

    A NEAREST-NEIGHBOUR SEARCH IN WORKSPACE A MUST NEVER RETURN, SCORE OR
    COUNT AN ISSUE IN WORKSPACE B -- INCLUDING, AND ESPECIALLY, WHEN B'S
    ISSUE IS THE BETTER MATCH.

That is the failure mode a vector index invites and a tsvector index does
not. An ANN structure ranks by distance over the whole table and knows
nothing about tenancy, so the natural implementation -- take the nearest k,
then filter by workspace -- gives a small tenant fewer results the more its
neighbours write, and leaks the shape of another tenant's corpus through the
count, the ordering and the latency. The tests below therefore do not merely
assert that B's rows are absent: they first PROVE that B's row is the nearest
neighbour globally, then assert that A's answer is bit-for-bit unchanged by
it -- and unchanged again after B is given fifty more near-perfect matches.

The rest of the file covers the schema decisions 025 argues for:

  * the `vector` extension is really installed, and the test fails loudly
    rather than skipping if it is not;
  * `issues.embedding_source_digest` is generated, so an edit cannot leave a
    stale embedding looking fresh;
  * the composite foreign key refuses a cross-tenant embedding row;
  * the regeneration queue is a query over that digest, so nothing has to
    remember to mark a row dirty;
  * a stale or wrong-model embedding contributes nothing to a search.

Marked `db`: deselected by default, skipped when Docker is unreachable.
"""

from uuid import UUID

import asyncpg
import pytest

from app.domain.semantic_search import EMBEDDING_DIMENSIONS, MIN_DUPLICATE_SIMILARITY
from app.domain.tenancy import WorkspaceScope
from app.repositories.embeddings import EmbeddingRepository
from app.repositories.issues import IssueRepository
from app.repositories.projects import ProjectRepository
from app.services.embeddings import HashingEmbedder, vector_literal
from app.services.search import SearchService

from tests.conftest import apply_all_migrations, reset_schema, seed_workflow_states


pytestmark = pytest.mark.db

WORKSPACE_A = UUID("00000000-0000-7000-8000-000000000001")
TEAM_A = UUID("00000000-0000-7000-8000-000000000002")

WORKSPACE_B = UUID("00000000-0000-7000-8000-0000000000a1")
TEAM_B = UUID("00000000-0000-7000-8000-0000000000a2")
TEAM_B_KEY = "ACME"

SCOPE_A = WorkspaceScope(workspace_id=WORKSPACE_A)
SCOPE_B = WorkspaceScope(workspace_id=WORKSPACE_B)

# The query every tenancy test below asks. Four words, so
# `websearch_to_tsquery` ANDs four stems -- which is what makes the lexical arm
# selective and lets the semantic arm show its own behaviour.
QUERY = "deploy pipeline flaky retries"

# Workspace B's issue: as close to QUERY as text gets without being it. This is
# the row that must never surface in workspace A, and the tests below prove it
# really is the nearest neighbour before asserting that A never sees it.
B_TITLE = "Deploy pipeline flaky retries"

INSERT_ISSUE_SQL = """
INSERT INTO issues (
    id, workspace_id, team_id, number, workflow_state_id, title, description
)
VALUES (
    $1, $2, $3, $4,
    (
        SELECT id FROM workflow_states
        WHERE workspace_id = $2 AND team_id = $3
        ORDER BY position
        LIMIT 1
    ),
    $5, $6
)
"""

# The nearest-neighbour read with NO tenant predicate, used only to establish
# what the ranking WOULD be. Nothing in the application issues a statement of
# this shape; it exists so that "workspace A did not see B's row" is a claim
# about the tenant predicate rather than about the data happening to be
# uninteresting.
GLOBAL_NEAREST_SQL = """
SELECT issue_id, 1 - (embedding <=> $1::vector) AS similarity
FROM issue_embeddings
ORDER BY embedding <=> $1::vector
LIMIT 5
"""

# (index, workspace, team, number, title, description)
#
# A's issues are deliberately WEAKER matches for QUERY than B's, and A holds
# one issue the lexical arm cannot reach at all -- issue 2 is missing the word
# "pipeline", so `websearch_to_tsquery`'s AND of four stems rejects it while
# the vector arm ranks it highly. That pair is what makes the hybrid tests
# below statements about the fusion rather than about the tsquery.
ISSUE_SEED = (
    (1, WORKSPACE_A, TEAM_A, 1, "Deploy pipeline flaky on retries", "It fails a lot"),
    (2, WORKSPACE_A, TEAM_A, 2, "Flaky deploy retries", "No mention of the other word"),
    (3, WORKSPACE_A, TEAM_A, 3, "Onboarding checklist", "Nothing to do with shipping"),
    # No description, deliberately: this row's embedding is then the query's
    # own, so it is the nearest neighbour in the installation by construction
    # rather than by luck -- which is the precondition every isolation test
    # below rests on.
    (4, WORKSPACE_B, TEAM_B, 1, B_TITLE, None),
)


def issue_id(index: int) -> UUID:
    return UUID(f"a1b2c3d4-0000-4000-9000-{index:012d}")


async def _seed(connection) -> None:
    await reset_schema(connection)
    await apply_all_migrations(connection)

    await connection.execute(
        "INSERT INTO workspaces (id, slug, name) VALUES ($1, $2, $3)",
        WORKSPACE_B,
        "acme",
        "Acme",
    )
    await connection.execute(
        "INSERT INTO teams (id, workspace_id, name, key) VALUES ($1, $2, $3, $4)",
        TEAM_B,
        WORKSPACE_B,
        "Acme Core",
        TEAM_B_KEY,
    )
    await seed_workflow_states(connection, WORKSPACE_B, TEAM_B)

    for index, workspace, team, number, title, description in ISSUE_SEED:
        await connection.execute(
            INSERT_ISSUE_SQL,
            issue_id(index),
            workspace,
            team,
            number,
            title,
            description,
        )


@pytest.fixture
async def pool(postgres_dsn):
    connection = await asyncpg.connect(postgres_dsn)

    try:
        await _seed(connection)
    finally:
        await connection.close()

    created = await asyncpg.create_pool(dsn=postgres_dsn, min_size=1, max_size=2)

    try:
        yield created
    finally:
        await created.close()


@pytest.fixture
def embedder() -> HashingEmbedder:
    return HashingEmbedder()


@pytest.fixture
def service(pool, embedder) -> SearchService:
    """The real service over the real repositories over the real database."""
    return SearchService(
        pool=pool,
        issue_repository=IssueRepository(),
        project_repository=ProjectRepository(),
        embedding_repository=EmbeddingRepository(),
        embedder=embedder,
    )


@pytest.fixture
def lexical_service(pool) -> SearchService:
    """The same service with no embedder, which is the degradation path."""
    return SearchService(
        pool=pool,
        issue_repository=IssueRepository(),
        project_repository=ProjectRepository(),
    )


@pytest.fixture
async def embedded(service) -> SearchService:
    """The service, with every seeded issue in BOTH workspaces embedded.

    Both, deliberately. A tenancy test whose other tenant has no embeddings
    proves nothing -- the rows have to exist and be nearer than the caller's
    own before "workspace A did not see them" means anything.
    """
    await service.refresh_embeddings(scope=SCOPE_A, limit=100)
    await service.refresh_embeddings(scope=SCOPE_B, limit=100)

    return service


# ------------------------------------------------------------ the extension


async def test_the_vector_extension_is_installed_and_not_skipped_past(pool):
    """The one thing this whole file would silently stop testing.

    Stock `postgres:18` does not ship pgvector, and the honest failure for a
    container without it is a red test rather than a skip: a green `pytest -m
    db` must not mean "the schema applies except for the extension nobody
    verified". tests/conftest.py pins `pgvector/pgvector:pg18` for this, and
    if that pin is ever reverted this assertion is what says so.
    """
    installed = await pool.fetchval(
        "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
    )

    assert installed is not None, (
        "pgvector is not installed in the test container; migration 025 cannot "
        "have applied, and every db suite in this repository runs "
        "apply_all_migrations"
    )


async def test_the_embedding_column_has_the_width_the_domain_declares(pool):
    """`vector(384)`, not a bare `vector`.

    A bare column accepts any width and defers the mismatch to the first
    comparison, where it surfaces as a runtime error on a query that used to
    work.
    """
    declared = await pool.fetchval(
        """
        SELECT format_type(atttypid, atttypmod)
        FROM pg_attribute
        WHERE attrelid = 'issue_embeddings'::regclass AND attname = 'embedding'
        """
    )

    assert declared == f"vector({EMBEDDING_DIMENSIONS})"


# --------------------------------------------------------------- the digest


async def test_the_digest_is_generated_and_cannot_be_written(pool):
    """The freshness mechanism, and the reason it is a column and not a flag.

    A generated column cannot be written by any statement at all, so there is
    no INSERT or UPDATE anywhere in this system -- now or later, from a
    webhook, a bulk import or a hand-run fix -- that can leave the digest
    disagreeing with the text it describes.
    """
    with pytest.raises(asyncpg.PostgresError):
        await pool.execute(
            "UPDATE issues SET embedding_source_digest = $1 WHERE id = $2",
            "0" * 64,
            issue_id(1),
        )


@pytest.mark.parametrize(
    ("column", "value"),
    [("title", "A different title"), ("description", "A different description")],
)
async def test_editing_either_field_moves_the_digest(pool, column, value):
    before = await pool.fetchval(
        "SELECT embedding_source_digest FROM issues WHERE id = $1", issue_id(1)
    )

    await pool.execute(
        f"UPDATE issues SET {column} = $1 WHERE id = $2", value, issue_id(1)
    )

    after = await pool.fetchval(
        "SELECT embedding_source_digest FROM issues WHERE id = $1", issue_id(1)
    )

    assert before != after


async def test_no_separator_can_make_two_different_issues_share_a_digest(pool):
    """Why the digest is two md5s and not one over a joined string.

    `md5(title || sep || description)` gives ("a" + sep, "b") and
    ("a", sep + "b") the same value, whatever `sep` is -- two different issues
    whose embeddings would then be interchangeable. Digesting each field and
    concatenating the results has no separator to confuse.
    """
    digests = []

    for title, description in (("ab", "c"), ("a", "bc"), ("a", "b\n\nc")):
        await pool.execute(
            "UPDATE issues SET title = $1, description = $2 WHERE id = $3",
            title,
            description,
            issue_id(3),
        )
        digests.append(
            await pool.fetchval(
                "SELECT embedding_source_digest FROM issues WHERE id = $1", issue_id(3)
            )
        )

    assert len(set(digests)) == len(digests)


# ---------------------------------------------------------- the constraints


async def test_an_embedding_cannot_name_another_workspaces_issue(pool):
    """The composite foreign key, which is the whole cross-tenant guarantee.

    One workspace_id column feeds both the primary key and the reference, so
    there is no column left for a second workspace to go in. A single-column
    `REFERENCES issues (id)` would accept this row.
    """
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await pool.execute(
            """
            INSERT INTO issue_embeddings
                (workspace_id, issue_id, model, source_digest, embedding)
            VALUES ($1, $2, 'x', $3, $4::vector)
            """,
            WORKSPACE_A,
            issue_id(4),
            "0" * 64,
            vector_literal([0.0] * (EMBEDDING_DIMENSIONS - 1) + [1.0]),
        )


async def test_a_digest_of_the_wrong_shape_is_refused(pool):
    """A digest that can only ever compare unequal is a row permanently stale.

    Nothing would report an error: the issue would simply be re-queued on
    every sweep, forever, and the search would never use its embedding.
    """
    with pytest.raises(asyncpg.CheckViolationError):
        await pool.execute(
            """
            INSERT INTO issue_embeddings
                (workspace_id, issue_id, model, source_digest, embedding)
            VALUES ($1, $2, 'x', 'not-a-digest', $3::vector)
            """,
            WORKSPACE_A,
            issue_id(1),
            vector_literal([0.0] * (EMBEDDING_DIMENSIONS - 1) + [1.0]),
        )


async def test_a_vector_of_the_wrong_width_is_refused(pool):
    with pytest.raises(asyncpg.PostgresError):
        await pool.execute(
            """
            INSERT INTO issue_embeddings
                (workspace_id, issue_id, model, source_digest, embedding)
            VALUES ($1, $2, 'x', $3, $4::vector)
            """,
            WORKSPACE_A,
            issue_id(1),
            "0" * 64,
            vector_literal([1.0, 0.0, 0.0]),
        )


# ---------------------------------------------------- the regeneration queue


async def test_every_live_issue_starts_in_the_queue(service):
    written = await service.refresh_embeddings(scope=SCOPE_A, limit=100)

    assert written == 3


async def test_a_drained_queue_reports_zero(service):
    await service.refresh_embeddings(scope=SCOPE_A, limit=100)

    assert await service.refresh_embeddings(scope=SCOPE_A, limit=100) == 0


async def test_editing_a_title_puts_the_issue_back_in_the_queue(embedded, pool):
    """NOTHING MARKS THE ROW DIRTY, which is the point.

    The UPDATE below is a bare statement against `issues` -- no service, no
    trigger, nothing that knows embeddings exist. The digest moves because it
    is generated, and the issue reappears in the queue because the queue is a
    join over that digest.
    """
    await pool.execute(
        "UPDATE issues SET title = 'Something else entirely' WHERE id = $1",
        issue_id(1),
    )

    assert await embedded.refresh_embeddings(scope=SCOPE_A, limit=100) == 1


async def test_the_queue_is_scoped_to_one_workspace(service):
    """A sweep in A embeds A's issues and leaves B's queued."""
    await service.refresh_embeddings(scope=SCOPE_A, limit=100)

    assert await service.refresh_embeddings(scope=SCOPE_B, limit=100) == 1


async def test_an_archived_issue_is_not_queued(service, pool):
    await pool.execute(
        "UPDATE issues SET archived_at = now() WHERE id = $1", issue_id(3)
    )

    assert await service.refresh_embeddings(scope=SCOPE_A, limit=100) == 2


async def test_a_write_is_declined_when_the_text_changed_underneath_it(pool):
    """The race the digest closes, forced by hand.

    A sweep reads the queue, the model runs, and in that window somebody
    renames the issue. Writing the vector anyway -- under the digest the sweep
    read -- would store a stale embedding stamped as current, which is exactly
    what this whole design exists to make impossible.
    """
    repository = EmbeddingRepository()
    stale_digest = await pool.fetchval(
        "SELECT embedding_source_digest FROM issues WHERE id = $1", issue_id(1)
    )

    await pool.execute(
        "UPDATE issues SET title = 'Renamed mid-sweep' WHERE id = $1", issue_id(1)
    )

    async with pool.acquire() as connection:
        stored = await repository.upsert(
            connection,
            scope=SCOPE_A,
            issue_id=issue_id(1),
            model="hashing-v1",
            source_digest=stale_digest,
            embedding=vector_literal([0.0] * (EMBEDDING_DIMENSIONS - 1) + [1.0]),
        )

    assert stored is False
    assert (
        await pool.fetchval(
            "SELECT count(*) FROM issue_embeddings WHERE issue_id = $1", issue_id(1)
        )
        == 0
    )


async def test_a_sweep_cannot_embed_another_workspaces_issue(pool):
    """The upsert's own tenant predicate, not just the queue's.

    A scope is an argument, so a caller holding workspace A's scope and
    workspace B's issue id is a shape the type system permits. The statement
    refuses it.
    """
    repository = EmbeddingRepository()
    digest = await pool.fetchval(
        "SELECT embedding_source_digest FROM issues WHERE id = $1", issue_id(4)
    )

    async with pool.acquire() as connection:
        stored = await repository.upsert(
            connection,
            scope=SCOPE_A,
            issue_id=issue_id(4),
            model="hashing-v1",
            source_digest=digest,
            embedding=vector_literal([0.0] * (EMBEDDING_DIMENSIONS - 1) + [1.0]),
        )

    assert stored is False


# ------------------------------------------------------------- the hybrid


async def test_the_hybrid_finds_what_the_lexical_search_alone_cannot(
    embedded, lexical_service
):
    """The reason this migration exists, in one comparison.

    Issue 2 does not contain "pipeline", and `websearch_to_tsquery` ANDs every
    stem, so the lexical search cannot return it for this query however
    similar it reads. The vector arm ranks it second and the fusion carries it
    into the results.
    """
    lexical = await lexical_service.search(scope=SCOPE_A, query=QUERY, first=10)
    hybrid = await embedded.search(scope=SCOPE_A, query=QUERY, first=10)

    assert issue_id(2) not in {issue.id for issue in lexical.issues}
    assert issue_id(2) in {issue.id for issue in hybrid.issues}


async def test_the_lexical_match_still_leads_the_fused_ordering(embedded):
    """Fusion adds a retriever; it does not overrule the one already there.

    Issue 1 is found by BOTH arms and near the top of each, so reciprocal rank
    fusion puts it first -- which is the behaviour a hybrid is for: agreement
    beats either arm's own confidence.
    """
    results = await embedded.search(scope=SCOPE_A, query=QUERY, first=10)

    assert results.issues[0].id == issue_id(1)


async def test_the_same_query_returns_the_same_order_every_time(embedded):
    """Reciprocal ranks tie constantly; `id DESC` is what makes the order total."""
    orders = set()

    for _ in range(10):
        results = await embedded.search(scope=SCOPE_A, query=QUERY, first=10)
        orders.add(tuple(issue.id for issue in results.issues))

    assert len(orders) == 1


async def test_a_stale_embedding_is_absent_rather_than_served(embedded, pool):
    """An issue ranked by a vector of text it no longer contains is ranked by a lie.

    After the rename, issue 2 is unreachable lexically (it never matched) and
    unreachable semantically (its embedding no longer matches its digest), so
    it drops out entirely -- rather than being returned at its old, wrong rank.
    """
    before = await embedded.search(scope=SCOPE_A, query=QUERY, first=10)

    await pool.execute(
        "UPDATE issues SET title = 'Completely unrelated now' WHERE id = $1",
        issue_id(2),
    )

    after = await embedded.search(scope=SCOPE_A, query=QUERY, first=10)

    assert issue_id(2) in {issue.id for issue in before.issues}
    assert issue_id(2) not in {issue.id for issue in after.issues}


async def test_an_embedding_under_another_model_is_not_compared(embedded, pool):
    """Two models are two coordinate systems, and a distance between them lies.

    Renaming the model on every stored row is what a model upgrade looks like
    from this query's point of view: nothing matches, the semantic arm returns
    nothing, and the search falls back to its lexical half rather than to
    nonsense.
    """
    await pool.execute("UPDATE issue_embeddings SET model = 'some-other-model'")

    results = await embedded.search(scope=SCOPE_A, query=QUERY, first=10)

    assert issue_id(2) not in {issue.id for issue in results.issues}
    assert issue_id(1) in {issue.id for issue in results.issues}


async def test_an_archived_issue_is_not_findable_by_the_hybrid_search(embedded, pool):
    await pool.execute(
        "UPDATE issues SET archived_at = now() WHERE id = $1", issue_id(1)
    )

    results = await embedded.search(scope=SCOPE_A, query=QUERY, first=10)

    assert issue_id(1) not in {issue.id for issue in results.issues}


# =====================================================================
# TENANT ISOLATION -- the property this file exists for
# =====================================================================


async def test_the_other_workspaces_issue_really_is_the_nearest_neighbour(
    embedded, pool, embedder
):
    """The precondition every test below depends on, established rather than assumed.

    Without this, "workspace A never returned B's issue" would be satisfied by
    a B whose issue was simply uninteresting. This asserts the opposite: with
    no tenant predicate, B's row is FIRST -- so every isolation assertion
    afterwards is a statement about the WHERE clause and not about the data.
    """
    embedded_query = vector_literal(embedder.embed([QUERY])[0])

    ranked = await pool.fetch(GLOBAL_NEAREST_SQL, embedded_query)

    assert ranked[0]["issue_id"] == issue_id(4)
    assert ranked[0]["similarity"] > ranked[1]["similarity"]


async def test_a_hybrid_search_never_returns_the_better_matching_foreign_issue(
    embedded,
):
    """THE TEST. Workspace B holds the best match in the installation.

    Workspace A must not see it -- not in the results, and not by any other
    route: its own results are exactly its own three issues' worth, ranked
    among themselves.
    """
    results = await embedded.search(scope=SCOPE_A, query=QUERY, first=10)
    found = {issue.id for issue in results.issues}

    assert issue_id(4) not in found
    assert found <= {issue_id(1), issue_id(2), issue_id(3)}


async def test_the_result_count_does_not_move_when_the_neighbour_grows(
    embedded, pool, service
):
    """THE POST-FILTER TEST, and the one an ANN top-k would fail.

    Fifty issues are added to workspace B, every one of them a near-perfect
    match for A's query -- so under "take the nearest k globally, then filter
    by workspace", A's twenty candidate slots would be filled with B's rows
    and A would be handed a shorter list, or an empty one, with nothing
    reporting an error.

    The assertion is equality of the WHOLE answer, ids and order, before and
    after. A count alone would miss a reordering, and a membership check would
    miss a truncation.
    """
    before = await embedded.search(scope=SCOPE_A, query=QUERY, first=20)

    state_b = await pool.fetchval(
        "SELECT id FROM workflow_states WHERE team_id = $1 ORDER BY position", TEAM_B
    )

    await pool.executemany(
        """
        INSERT INTO issues (
            id, workspace_id, team_id, number, workflow_state_id, title, description
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        """,
        [
            (
                issue_id(500 + n),
                WORKSPACE_B,
                TEAM_B,
                100 + n,
                state_b,
                B_TITLE,
                None,
            )
            for n in range(50)
        ],
    )

    assert await service.refresh_embeddings(scope=SCOPE_B, limit=100) == 50

    after = await embedded.search(scope=SCOPE_A, query=QUERY, first=20)

    assert [issue.id for issue in after.issues] == [issue.id for issue in before.issues]


async def test_duplicate_suggestions_never_score_a_foreign_issue(embedded):
    """The score is the disclosure here, so it is what is asserted about.

    A suggestion carries a similarity. Handing workspace A a similarity
    computed against workspace B's text would disclose that B holds something
    close to what A is writing -- which is a leak even with the issue's id,
    title and body withheld. A's best similarity for B's own title must be its
    OWN issue's, and B's must be higher.
    """
    from_a = await embedded.suggest_duplicates(
        scope=SCOPE_A,
        title=B_TITLE,
        description=None,
        exclude_issue_id=None,
        first=10,
    )
    from_b = await embedded.suggest_duplicates(
        scope=SCOPE_B,
        title=B_TITLE,
        description=None,
        exclude_issue_id=None,
        first=10,
    )

    assert issue_id(4) not in {suggestion.issue.id for suggestion in from_a}
    assert issue_id(4) in {suggestion.issue.id for suggestion in from_b}

    # The mirror of the ranking test above: B's own answer scores higher than
    # anything A can see, which is what makes A's silence meaningful.
    assert max(suggestion.similarity for suggestion in from_b) > max(
        suggestion.similarity for suggestion in from_a
    )


async def test_a_foreign_exclusion_id_changes_nothing(embedded):
    """`excludeIssueId` only ever removes a row from an already-scoped list.

    So an id from another tenant excludes nothing, and the answer is identical
    to having passed none -- which is what makes it safe not to authorize the
    parameter separately. If the two answers differed, the field would be an
    oracle for whether a guessed id exists elsewhere.
    """
    without = await embedded.suggest_duplicates(
        scope=SCOPE_A,
        title=B_TITLE,
        description=None,
        exclude_issue_id=None,
        first=10,
    )
    with_foreign = await embedded.suggest_duplicates(
        scope=SCOPE_A,
        title=B_TITLE,
        description=None,
        exclude_issue_id=issue_id(4),
        first=10,
    )

    assert [suggestion.issue.id for suggestion in with_foreign] == [
        suggestion.issue.id for suggestion in without
    ]


# ------------------------------------------------------ duplicate suggestions


async def test_the_duplicate_of_an_issue_is_the_issue_that_says_the_same_thing(
    embedded,
):
    suggestions = await embedded.suggest_duplicates(
        scope=SCOPE_A,
        title="Deploy pipeline flaky on retries",
        description=None,
        exclude_issue_id=None,
        first=5,
    )

    assert suggestions[0].issue.id == issue_id(1)
    assert suggestions[0].similarity >= MIN_DUPLICATE_SIMILARITY


async def test_an_issue_is_never_suggested_as_its_own_duplicate(embedded):
    """The editing case: the text matches itself perfectly, which is useless."""
    suggestions = await embedded.suggest_duplicates(
        scope=SCOPE_A,
        title="Deploy pipeline flaky on retries",
        description="It fails a lot",
        exclude_issue_id=issue_id(1),
        first=5,
    )

    assert issue_id(1) not in {suggestion.issue.id for suggestion in suggestions}


async def test_unrelated_text_suggests_nothing_rather_than_the_nearest_row(embedded):
    """A cosine is defined for every pair, so without a floor there is always a
    "most similar issue" -- and an interface that always claims to have found a
    possible duplicate is one people stop reading."""
    suggestions = await embedded.suggest_duplicates(
        scope=SCOPE_A,
        title="Quarterly budget spreadsheet needs a new tab",
        description=None,
        exclude_issue_id=None,
        first=5,
    )

    assert suggestions == []


async def test_every_suggestion_clears_the_floor(embedded):
    suggestions = await embedded.suggest_duplicates(
        scope=SCOPE_A,
        title=B_TITLE,
        description=None,
        exclude_issue_id=None,
        first=10,
    )

    assert suggestions
    assert all(
        suggestion.similarity >= MIN_DUPLICATE_SIMILARITY for suggestion in suggestions
    )


async def test_an_archived_issue_is_never_suggested_as_a_duplicate(embedded, pool):
    await pool.execute(
        "UPDATE issues SET archived_at = now() WHERE id = $1", issue_id(1)
    )

    suggestions = await embedded.suggest_duplicates(
        scope=SCOPE_A,
        title="Deploy pipeline flaky on retries",
        description=None,
        exclude_issue_id=None,
        first=5,
    )

    assert issue_id(1) not in {suggestion.issue.id for suggestion in suggestions}
