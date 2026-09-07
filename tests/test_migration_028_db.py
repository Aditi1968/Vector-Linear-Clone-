"""Migration 028 and the embedding lifecycle, against a real PostgreSQL.

tests/test_migration_025_db.py proves that the READ path is tenant-safe and
that a stale embedding is never served. It runs its sweeps by hand, because
when it was written nothing ran them at all: `embeddingsRefresh` was a mutation
somebody had to call, nobody called it, and a fresh install therefore had no
embeddings and answered every duplicate check with "no duplicates found" when
the truth was "no index".

This file is about the half that closes that, and there are five claims in it
that matter more than the rest:

  * TWO WORKERS CLAIMING AT THE SAME INSTANT NEVER TAKE THE SAME ROW. Not
    "usually", and not "the second one notices afterwards" -- the test below
    holds one worker's transaction open while the other claims, so the skip is
    forced rather than hoped for;
  * AN EDITED TITLE IS RE-EMBEDDED, without anything having marked it dirty;
  * A POISON ROW STOPS. An issue whose text makes the embedder raise is retried
    a bounded number of times and then never again, and it does not take its
    batch-mates down with it;
  * THE INDEXING STATE IS TRUE. "Still building, N pending" and "nothing
    similar" are different answers and the counts distinguish them;
  * NOTHING LEAKS THROUGH THE QUEUE. The worker is the one reader in this
    system with no tenant of its own, so every isolation property has to come
    from the row it read rather than from a caller.

Marked `db`: deselected by default, skipped when Docker is unreachable.
"""

from uuid import UUID

import asyncpg
import pytest

from app.domain.embedding_jobs import MAX_ATTEMPTS
from app.domain.tenancy import WorkspaceScope
from app.repositories.embedding_jobs import EmbeddingJobRepository
from app.repositories.embeddings import EmbeddingRepository
from app.repositories.issues import IssueRepository
from app.repositories.projects import ProjectRepository
from app.services.embedding_jobs import EmbeddingWorker
from app.services.embeddings import HashingEmbedder
from app.services.search import SearchService

from tests.conftest import apply_all_migrations, reset_schema, seed_workflow_states


pytestmark = pytest.mark.db

# The bootstrap tenant migrations/002_tenancy.sql seeds, reused rather than
# inserted: an issue has to hang off a team that has workflow states, and 002
# already built one.
WORKSPACE_A = UUID("00000000-0000-7000-8000-000000000001")
TEAM_A = UUID("00000000-0000-7000-8000-000000000002")

WORKSPACE_B = UUID("00000000-0000-7000-8000-0000000000a1")
TEAM_B = UUID("00000000-0000-7000-8000-0000000000a2")
TEAM_B_KEY = "ACME"

SCOPE_A = WorkspaceScope(workspace_id=WORKSPACE_A)
SCOPE_B = WorkspaceScope(workspace_id=WORKSPACE_B)

# The title `PoisonEmbedder` below refuses to embed. A whole string rather than
# a substring anybody could type by accident, so that exactly one seeded issue
# is poisoned and the other two prove that its failure did not spread.
POISON_TITLE = "The title this deployment's model cannot tokenise"

# (index, workspace, team, number, title, description)
#
# Three issues in A and one in B, and B's is deliberately the closest text to
# A's first: the worker claims across every workspace at once, so "A's
# embeddings are A's" has to be a claim about a run that really did handle both.
ISSUE_SEED = (
    (1, WORKSPACE_A, TEAM_A, 1, "Deploy pipeline flaky on retries", "It fails a lot"),
    (2, WORKSPACE_A, TEAM_A, 2, "Onboarding checklist", None),
    (3, WORKSPACE_A, TEAM_A, 3, POISON_TITLE, None),
    (4, WORKSPACE_B, TEAM_B, 1, "Deploy pipeline flaky retries", None),
)

# How many live issues workspace A holds. The three counts of `IndexingState`
# partition exactly this set, so every assertion about them sums to it.
A_LIVE_ISSUES = 3

# Every job due right now, whatever backoff a failure just wrote.
#
# Time travel, and it is the honest kind: the backoff is minutes and a test is
# milliseconds, so a poison row's SECOND attempt would otherwise never be
# observable at all. Nothing about the bound depends on the wait, which is the
# property under test -- see `test_a_poison_row_stops_at_its_retry_bound`, which
# resets the clock and still finds the row retired.
MAKE_EVERY_JOB_DUE_SQL = "UPDATE embedding_jobs SET next_attempt_at = now()"

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

# Every embedding whose row claims one workspace while its issue lives in
# another. The answer must be zero and there must be no way to make it
# otherwise; the composite foreign key is what enforces that, and this is the
# read that would notice if the worker ever wrote a scope it had not read off
# the claimed row.
MISFILED_EMBEDDINGS_SQL = """
SELECT count(*)
FROM issue_embeddings
JOIN issues ON issues.id = issue_embeddings.issue_id
WHERE issues.workspace_id <> issue_embeddings.workspace_id
"""


def issue_id(index: int) -> UUID:
    return UUID(f"a1b2c3d4-0000-4000-9000-{index:012d}")


class PoisonEmbedder(HashingEmbedder):
    """The real hashing embedder, except that one string makes it raise.

    A subclass rather than a mock, so everything except the one failure is the
    genuine embedder: the vectors the healthy issues get are real vectors, and
    the model NAME is unchanged, which matters because the name is what every
    read filters on. A fake returning fixed vectors would let a broken write
    path pass.

    It raises for a BATCH containing the poisoned text as well as for the text
    alone, which is what a real tokeniser failure does -- and is precisely the
    behaviour that would charge an attempt to all of an innocent batch if
    `EmbeddingWorker._embed` did not fall back to embedding one at a time.
    """

    def embed(self, texts):
        for text in texts:
            if text.startswith(POISON_TITLE):
                raise RuntimeError("cannot tokenise this")

        return super().embed(texts)


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
def jobs() -> EmbeddingJobRepository:
    return EmbeddingJobRepository()


@pytest.fixture
def embedder() -> HashingEmbedder:
    return HashingEmbedder()


@pytest.fixture
def worker(pool, embedder) -> EmbeddingWorker:
    """The real worker over the real repositories over the real database."""
    return EmbeddingWorker(
        pool=pool,
        jobs=EmbeddingJobRepository(),
        embeddings=EmbeddingRepository(),
        embedder=embedder,
    )


@pytest.fixture
def poison_worker(pool) -> EmbeddingWorker:
    return EmbeddingWorker(
        pool=pool,
        jobs=EmbeddingJobRepository(),
        embeddings=EmbeddingRepository(),
        embedder=PoisonEmbedder(),
    )


@pytest.fixture
def service(pool, embedder) -> SearchService:
    return SearchService(
        pool=pool,
        issue_repository=IssueRepository(),
        project_repository=ProjectRepository(),
        embedding_repository=EmbeddingRepository(),
        embedder=embedder,
        job_repository=EmbeddingJobRepository(),
    )


@pytest.fixture
def lexical_service(pool) -> SearchService:
    """The same service with no embedder, which is the degradation path."""
    return SearchService(
        pool=pool,
        issue_repository=IssueRepository(),
        project_repository=ProjectRepository(),
    )


# ------------------------------------------------------- the fresh install


async def test_a_fresh_install_reports_a_pending_index_rather_than_nothing(
    service,
):
    """The whole reason this migration exists, stated as an assertion.

    Before the worker, a fresh install had no embeddings and no way to say so:
    `issueDuplicateSuggestions` answered `[]`, which a client could only render
    as "no possible duplicates" about a workspace that had never been indexed.
    """
    state = await service.indexing_state(scope=SCOPE_A)

    assert state.enabled is True
    assert state.pending == A_LIVE_ISSUES
    assert state.indexed == 0
    assert state.failed == 0


async def test_one_pass_indexes_every_workspace_and_reports_it_truthfully(
    worker, service
):
    """One `run_once` fills the index in, and the counts then say so.

    The worker has no tenant, so its single pass covers BOTH workspaces -- four
    issues, not workspace A's three -- while each workspace's own state read
    counts only its own.
    """
    written = await worker.run_once()

    assert written == len(ISSUE_SEED)

    state = await service.indexing_state(scope=SCOPE_A)

    assert state.indexed == A_LIVE_ISSUES
    assert state.pending == 0
    assert state.failed == 0

    other = await service.indexing_state(scope=SCOPE_B)

    assert other.indexed == 1


async def test_a_second_pass_over_a_full_index_does_nothing_at_all(worker):
    """Idempotent, and cheaply so: the anti-join produces nothing to queue."""
    await worker.run_once()

    assert await worker.run_once() == 0


async def test_the_state_is_disabled_and_empty_without_an_embedder(
    worker, lexical_service
):
    """A deployment with no model says so, rather than reporting a full index.

    Run AFTER a real pass, deliberately: the rows exist, so a state read that
    merely counted them would answer "3 indexed" -- and a client would render a
    complete index for a deployment whose semantic search cannot run. Freshness
    is defined against the model doing the asking, and there is no model here.
    """
    await worker.run_once()

    state = await lexical_service.indexing_state(scope=SCOPE_A)

    assert state.enabled is False
    assert (state.indexed, state.pending, state.failed) == (0, 0, 0)


# --------------------------------------------------------------- staleness


async def test_an_edited_title_goes_stale_and_is_re_embedded(pool, worker, service):
    """Nothing marks the row dirty, and nothing has to.

    The digest is a generated column, so the UPDATE that changes the title
    changes it in the same statement; the stored vector stops matching, the
    anti-join produces the issue again, and the next pass replaces the vector.
    No writer anywhere -- not this UPDATE, not a bulk import, not a webhook --
    had to know the embedding existed.

    The stored vector is compared before and after, not just its digest. A pass
    that rewrote the digest and kept the old vector would satisfy every
    freshness predicate in the schema while serving an embedding of text that no
    longer exists, which is the exact failure 025's digest column exists to make
    impossible.
    """
    await worker.run_once()

    before = await pool.fetchval(
        "SELECT embedding::text FROM issue_embeddings WHERE issue_id = $1",
        issue_id(1),
    )

    await pool.execute(
        "UPDATE issues SET title = $2 WHERE id = $1",
        issue_id(1),
        "Billing invoices are rounded to the wrong currency unit",
    )

    stale = await service.indexing_state(scope=SCOPE_A)

    assert stale.pending == 1
    assert stale.indexed == A_LIVE_ISSUES - 1

    assert await worker.run_once() == 1

    fresh = await service.indexing_state(scope=SCOPE_A)

    assert fresh.pending == 0
    assert fresh.indexed == A_LIVE_ISSUES

    after = await pool.fetchval(
        "SELECT embedding::text FROM issue_embeddings WHERE issue_id = $1",
        issue_id(1),
    )

    assert after != before

    matches = await pool.fetchval(
        """
        SELECT issue_embeddings.source_digest = issues.embedding_source_digest
        FROM issue_embeddings
        JOIN issues ON issues.id = issue_embeddings.issue_id
        WHERE issues.id = $1
        """,
        issue_id(1),
    )

    assert matches is True


async def test_an_archived_issue_is_never_queued_and_never_counted(pool, worker, jobs):
    """`archived_at IS NULL` is in the anti-join, so archiving is enough.

    No hook, no cleanup pass and no cascade: an archived issue simply stops
    being produced by the query that decides what needs embedding.
    """
    await pool.execute(
        "UPDATE issues SET archived_at = now() WHERE id = $1",
        issue_id(2),
    )

    await worker.run_once()

    queued = await pool.fetchval(
        "SELECT count(*) FROM embedding_jobs WHERE issue_id = $1",
        issue_id(2),
    )

    assert queued == 0

    embedded = await pool.fetchval(
        "SELECT count(*) FROM issue_embeddings WHERE issue_id = $1",
        issue_id(2),
    )

    assert embedded == 0


async def test_deleting_an_issue_takes_its_queued_attempt_with_it(pool, jobs, embedder):
    """ON DELETE CASCADE, and the one place this feature departs from 025.

    025 gives `issue_embeddings` a RESTRICT, because an embedding is DATA and a
    one-line delete must not discard it invisibly. An attempt row is not data --
    it is a note saying somebody should embed this issue -- and an issue that no
    longer exists is one nobody should embed, so RESTRICT here would mean the
    first `DELETE FROM issues` this product grows failing on a row whose only
    correct handling is to vanish.
    """
    async with pool.acquire() as connection:
        await jobs.enqueue_stale(connection, model=embedder.name, limit=100)

    assert await pool.fetchval("SELECT count(*) FROM embedding_jobs") == len(ISSUE_SEED)

    await pool.execute("DELETE FROM issues WHERE id = $1", issue_id(2))

    surviving = await pool.fetchval(
        "SELECT count(*) FROM embedding_jobs WHERE issue_id = $1",
        issue_id(2),
    )

    assert surviving == 0


# ------------------------------------------------------------- concurrency


async def test_two_workers_claiming_at_once_never_take_the_same_row(
    pool, jobs, embedder
):
    """THE CLAIM IS DISJOINT, and this test forces the race rather than racing.

    The first claim runs inside a transaction that is deliberately still OPEN
    when the second one runs, so the second is looking at rows the first holds
    row locks on. That is the exact instant `FOR UPDATE SKIP LOCKED` exists for:
    without SKIP LOCKED the second worker blocks until the first commits and
    then claims the very same rows, which is how a queue table comes to process
    everything twice.

    Two separate connections, because two transactions are what makes locks mean
    anything -- the same connection would simply be one transaction.

    Asserted three ways, because "no duplicates" alone would also be satisfied
    by the second worker claiming nothing at all: the two sets are disjoint,
    neither is empty, and together they are every queued row exactly once.
    """
    async with pool.acquire() as connection:
        queued = await jobs.enqueue_stale(connection, model=embedder.name, limit=100)

    assert queued == len(ISSUE_SEED)

    async with pool.acquire() as first_connection:
        async with pool.acquire() as second_connection:
            async with first_connection.transaction():
                first = await jobs.claim(first_connection, limit=2, lease_seconds=300.0)

                # Inside the first transaction, on purpose. The rows above are
                # locked at this moment, and nothing has been committed.
                async with second_connection.transaction():
                    second = await jobs.claim(
                        second_connection, limit=100, lease_seconds=300.0
                    )

    taken_by_first = {job.source.issue_id for job in first}
    taken_by_second = {job.source.issue_id for job in second}

    assert len(first) == 2
    assert second, "the second worker was blocked instead of skipping"
    assert taken_by_first.isdisjoint(taken_by_second)
    assert taken_by_first | taken_by_second == {
        issue_id(index) for index, *_ in ISSUE_SEED
    }


async def test_a_leased_row_is_invisible_to_the_next_claim(pool, jobs, embedder):
    """The lease outlives the transaction that took it.

    The row lock ends at commit -- a fraction of a second later, because a
    worker releases its connection before running the model -- so if the lease
    were only the lock, a second worker arriving a moment afterwards would claim
    rows the first is still embedding and pay full model time for a vector
    already being computed. `next_attempt_at` in the future is what makes the
    claim outlast the transaction.
    """
    async with pool.acquire() as connection:
        await jobs.enqueue_stale(connection, model=embedder.name, limit=100)

        async with connection.transaction():
            first = await jobs.claim(connection, limit=100, lease_seconds=300.0)

        assert len(first) == len(ISSUE_SEED)

        async with connection.transaction():
            second = await jobs.claim(connection, limit=100, lease_seconds=300.0)

    assert second == []


# ------------------------------------------------------------ the poison row


async def test_a_poison_row_stops_at_its_retry_bound(pool, poison_worker, service):
    """One unembeddable issue costs a bounded number of attempts and then none.

    THE FAILURE THIS PREVENTS is an anti-join with no memory: an issue whose
    text makes the embedder raise is produced by it forever, so it consumes a
    slot in every sweep this installation ever runs and its neighbours are
    always behind it.

    Two things are asserted and both matter. The bound holds -- after
    MAX_ATTEMPTS failures the row is retired and a further pass does not touch
    it, even though the clock has been wound forward between every earlier
    attempt. And THE FAILURE DID NOT SPREAD: the two healthy issues in the same
    batch are embedded, which they would not be if a batch that raises as a unit
    charged an attempt to everything in it.
    """
    # The reset comes BEFORE each pass, never after. Winding the clock forward
    # after the last one would overwrite the very 'infinity' this asserts.
    for _ in range(MAX_ATTEMPTS):
        await pool.execute(MAKE_EVERY_JOB_DUE_SQL)
        await poison_worker.run_once()

    row = await pool.fetchrow(
        """
        SELECT attempts, next_attempt_at = 'infinity'::timestamptz AS retired
        FROM embedding_jobs
        WHERE issue_id = $1
        """,
        issue_id(3),
    )

    assert row["attempts"] == MAX_ATTEMPTS
    assert row["retired"] is True

    # And now with no reset, which is what a real worker sees.
    assert await poison_worker.run_once() == 0

    unchanged = await pool.fetchval(
        "SELECT attempts FROM embedding_jobs WHERE issue_id = $1",
        issue_id(3),
    )

    assert unchanged == MAX_ATTEMPTS

    state = await service.indexing_state(scope=SCOPE_A)

    assert state.failed == 1
    assert state.indexed == A_LIVE_ISSUES - 1
    assert state.pending == 0


async def test_a_retired_row_is_tried_again_once_its_text_changes(
    pool, poison_worker, worker, service
):
    """A bound on the OLD text says nothing about the new text.

    Refusing to try an edited issue because a previous version of it was poison
    would be punishing an issue for its own history -- and it is the state a
    queue with a `status = 'failed'` column gets stuck in. Here the reset falls
    out of the digest: the queued attempt is about text the issue no longer has,
    so the enqueue sweep replaces it with `attempts` back at zero.
    """
    # The reset comes BEFORE each pass, never after. Winding the clock forward
    # after the last one would overwrite the very 'infinity' this asserts.
    for _ in range(MAX_ATTEMPTS):
        await pool.execute(MAKE_EVERY_JOB_DUE_SQL)
        await poison_worker.run_once()

    assert (await service.indexing_state(scope=SCOPE_A)).failed == 1

    await pool.execute(
        "UPDATE issues SET title = $2 WHERE id = $1",
        issue_id(3),
        "A title the model can read perfectly well",
    )

    revived = await service.indexing_state(scope=SCOPE_A)

    assert revived.failed == 0
    assert revived.pending == 1

    assert await worker.run_once() == 1
    assert (await service.indexing_state(scope=SCOPE_A)).indexed == A_LIVE_ISSUES


async def test_a_poisoned_batch_does_not_charge_its_innocent_members(
    pool, poison_worker
):
    """The healthy issues in a failed batch keep an attempt count of zero.

    Stated separately from the bound, because it is the property that decides
    whether the bound is aimed at the right row. `Embedder.embed` takes a
    sequence and a real model fails as a unit, so a worker that charged the
    batch would retire four healthy issues for a defect in the fifth -- and the
    installation would slowly stop indexing anything.
    """
    await poison_worker.run_once()

    rows = await pool.fetchval("SELECT count(*) FROM embedding_jobs WHERE attempts > 0")

    assert rows == 1

    embedded = await pool.fetchval("SELECT count(*) FROM issue_embeddings")

    assert embedded == len(ISSUE_SEED) - 1


# ---------------------------------------------------------------- tenancy


async def test_the_queue_cannot_hold_one_tenants_issue_under_anothers_id(pool):
    """The composite foreign key, which is what makes a claimed row trustworthy.

    A worker has no tenant: it claims across every workspace at once and the
    workspace it then writes an embedding for is the `workspace_id` it read off
    the claimed row. That read is only safe if such a row cannot pair one
    workspace's tenancy column with another's issue -- so this asserts that it
    cannot, rather than trusting that no code path would write one.

    A single-column `REFERENCES issues (id)` would accept this row and every
    constraint would report success.
    """
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await pool.execute(
            """
            INSERT INTO embedding_jobs (workspace_id, issue_id, source_digest)
            VALUES ($1, $2, repeat('a', 64))
            """,
            WORKSPACE_A,
            issue_id(4),
        )


async def test_a_worker_draining_both_tenants_files_every_vector_correctly(
    pool, worker, service
):
    """One untenanted pass, and no embedding lands in the wrong workspace.

    The worker is the single place in this system that writes across tenants, so
    this is the read that would catch a scope built from anything other than the
    claimed row -- an ambient default, a caller's argument, the last workspace
    seen in the loop.
    """
    await worker.run_once()

    assert await pool.fetchval(MISFILED_EMBEDDINGS_SQL) == 0

    # And the client-facing count is scoped too: A holds three of the four
    # issues the pass embedded, and reports three.
    assert (await service.indexing_state(scope=SCOPE_A)).indexed == A_LIVE_ISSUES
    assert (await service.indexing_state(scope=SCOPE_B)).indexed == 1


async def test_one_tenants_backlog_is_not_reported_as_anothers(pool, service):
    """B's unindexed issues are not A's pending count, and never were.

    A count is exactly the shape a cross-tenant defect hides in: nothing is
    returned that could be inspected, so a filter applied after the aggregate
    would be invisible in the result and visible only as a number that moved
    when another tenant wrote something.
    """
    for number in range(2, 40):
        await pool.execute(
            INSERT_ISSUE_SQL,
            issue_id(100 + number),
            WORKSPACE_B,
            TEAM_B,
            number,
            f"Another unindexed issue in the other workspace {number}",
            None,
        )

    state = await service.indexing_state(scope=SCOPE_A)

    assert state.pending == A_LIVE_ISSUES
    assert state.indexed == 0
    assert state.failed == 0
