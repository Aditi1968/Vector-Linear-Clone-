"""The embedding lifecycle's decisions, made before any statement is issued.

tests/test_migration_028_db.py is about the SQL -- the claim, the bound, the
counts. This file is about what the worker decides on its own: the backoff
arithmetic, that a deployment with no model does no work rather than failing,
that a batch which fails as a unit is retried one text at a time, and that a
declined write is finished rather than retried.

No database, no Docker, and no `db` marker: every test here runs in the default
pass.
"""

from uuid import UUID, uuid4

import pytest

from app.domain.embedding_jobs import (
    BASE_RETRY_SECONDS,
    LEASE_SECONDS,
    MAX_ATTEMPTS,
    MAX_RETRY_SECONDS,
    ClaimedEmbeddingJob,
    IndexingState,
    retry_delay_seconds,
)
from app.domain.semantic_search import EMBEDDING_DIMENSIONS, EmbeddingSource
from app.services.embedding_jobs import CLAIM_BATCH, ENQUEUE_BATCH, EmbeddingWorker
from app.services.embeddings import HashingEmbedder

from tests.conftest import FakePool


WORKSPACE = UUID("00000000-0000-7000-8000-0000000000b1")

POISON = "unembeddable"


def job(title: str, *, attempts: int = 0) -> ClaimedEmbeddingJob:
    return ClaimedEmbeddingJob(
        workspace_id=WORKSPACE,
        attempts=attempts,
        source=EmbeddingSource(
            issue_id=uuid4(),
            source_digest="a" * 64,
            title=title,
            description=None,
        ),
    )


class BreakingEmbedder(HashingEmbedder):
    """The real embedder, except one string raises -- alone or in a batch.

    Counts its calls, because the property under test is not only "the healthy
    texts were embedded" but "the batch was tried first and only then retried
    one at a time". A worker that always embedded singly would satisfy every
    assertion about outcomes while throwing away the reason `Embedder.embed`
    takes a sequence.
    """

    def __init__(self):
        super().__init__()

        self.calls: list[int] = []

    def embed(self, texts):
        self.calls.append(len(texts))

        for text in texts:
            if POISON in text:
                raise RuntimeError("cannot tokenise this")

        return super().embed(texts)


class RecordingJobRepository:
    """Enough of `EmbeddingJobRepository` to watch what the worker decides.

    Mirrors the real signatures exactly, so a change to one of them fails here
    rather than letting a fake drift into agreeing with nothing.
    """

    def __init__(self, claimed: list[ClaimedEmbeddingJob] | None = None):
        self.claimed = claimed if claimed is not None else []
        self.enqueue_calls: list[dict] = []
        self.claim_calls: list[dict] = []
        self.completed: list[UUID] = []
        self.failures: list[dict] = []

    async def enqueue_stale(self, connection, *, model, limit) -> int:
        self.enqueue_calls.append({"model": model, "limit": limit})

        return len(self.claimed)

    async def claim(self, connection, *, limit, lease_seconds):
        self.claim_calls.append({"limit": limit, "lease_seconds": lease_seconds})

        taken = self.claimed
        # Drained, so a worker that claimed twice in one pass would be visible
        # as an empty second batch rather than as the same work done twice.
        self.claimed = []

        return taken

    async def complete(self, connection, *, workspace_id, issue_id) -> None:
        self.completed.append(issue_id)

    async def fail(
        self,
        connection,
        *,
        workspace_id,
        issue_id,
        error,
        max_attempts,
        retry_after_seconds,
    ) -> None:
        self.failures.append(
            {
                "issue_id": issue_id,
                "error": error,
                "max_attempts": max_attempts,
                "retry_after_seconds": retry_after_seconds,
            }
        )


class RecordingEmbeddingRepository:
    """`EmbeddingRepository.upsert`, and whether it accepted the write.

    `stored` is what the real one returns when its own WHERE clause declines --
    the issue was archived, or its text changed while the model ran -- which is
    the case the worker has to treat as finished rather than as a failure.
    """

    def __init__(self, stored: bool = True):
        self.stored = stored
        self.upserts: list[dict] = []

    async def upsert(
        self,
        connection,
        *,
        scope,
        issue_id,
        model,
        source_digest,
        embedding,
    ) -> bool:
        self.upserts.append(
            {
                "scope": scope,
                "issue_id": issue_id,
                "model": model,
                "source_digest": source_digest,
                "embedding": embedding,
            }
        )

        return self.stored


def build_worker(
    *,
    claimed: list[ClaimedEmbeddingJob] | None = None,
    embedder=None,
    stored: bool = True,
) -> tuple[EmbeddingWorker, RecordingJobRepository, RecordingEmbeddingRepository]:
    jobs = RecordingJobRepository(claimed)
    embeddings = RecordingEmbeddingRepository(stored)
    worker = EmbeddingWorker(
        pool=FakePool(),
        jobs=jobs,
        embeddings=embeddings,
        embedder=HashingEmbedder() if embedder is None else embedder,
    )

    return worker, jobs, embeddings


# ------------------------------------------------------------- the backoff


def test_the_first_retry_waits_rather_than_spinning():
    """A job that failed and is immediately due again is a spin, not a retry."""
    assert retry_delay_seconds(1) == BASE_RETRY_SECONDS


def test_the_backoff_doubles():
    delays = [retry_delay_seconds(attempts) for attempts in range(1, 5)]

    assert delays == [
        BASE_RETRY_SECONDS,
        BASE_RETRY_SECONDS * 2,
        BASE_RETRY_SECONDS * 4,
        BASE_RETRY_SECONDS * 8,
    ]


def test_the_backoff_is_capped():
    """Doubling without a ceiling reaches days, which is indistinguishable from
    never -- and `MAX_ATTEMPTS` already expresses "never" honestly."""
    assert retry_delay_seconds(50) == MAX_RETRY_SECONDS


# --------------------------------------------------------- the degradation


async def test_a_deployment_with_no_model_does_no_work_and_says_so():
    """The requirement the whole embedder design exists for.

    No model installed must mean the application boots, serves, and degrades to
    lexical search -- not that a background task raises on every pass. The check
    is the WIRE being None rather than an exception being caught, so this also
    asserts that nothing was claimed: a pass that queued work and then could not
    do it would leave rows leased for nothing.
    """
    jobs = RecordingJobRepository([job("anything")])
    worker = EmbeddingWorker(
        pool=FakePool(),
        jobs=jobs,
        embeddings=RecordingEmbeddingRepository(),
        embedder=None,
    )

    assert await worker.run_once() == 0
    assert jobs.enqueue_calls == []
    assert jobs.claim_calls == []


async def test_an_empty_queue_costs_one_claim_and_stops():
    """The common case on a steady-state installation, and it must be cheap."""
    worker, jobs, embeddings = build_worker(claimed=[])

    assert await worker.run_once() == 0
    assert embeddings.upserts == []


# ---------------------------------------------------------------- the pass


async def test_a_pass_embeds_what_it_claimed_and_deletes_the_job():
    worker, jobs, embeddings = build_worker(claimed=[job("Deploy pipeline is flaky")])

    assert await worker.run_once() == 1
    assert len(embeddings.upserts) == 1
    assert jobs.completed == [embeddings.upserts[0]["issue_id"]]
    assert jobs.failures == []


async def test_the_scope_comes_off_the_claimed_row():
    """A worker has no viewer, so the workspace it writes to has to be the one
    it read -- never an ambient default and never a caller's argument."""
    worker, _, embeddings = build_worker(claimed=[job("Deploy pipeline is flaky")])

    await worker.run_once()

    assert embeddings.upserts[0]["scope"].workspace_id == WORKSPACE


async def test_the_vector_is_written_in_pgvector_literal_form():
    worker, _, embeddings = build_worker(claimed=[job("Deploy pipeline is flaky")])

    await worker.run_once()

    literal = embeddings.upserts[0]["embedding"]

    assert literal.startswith("[")
    assert literal.endswith("]")
    assert len(literal.split(",")) == EMBEDDING_DIMENSIONS


async def test_the_pass_uses_the_batch_sizes_the_service_declares():
    """The claim budget is much smaller than the enqueue budget on purpose:
    every claimed row is invisible to every other worker for the lease."""
    worker, jobs, _ = build_worker(claimed=[job("Deploy pipeline is flaky")])

    await worker.run_once()

    assert jobs.enqueue_calls[0]["limit"] == ENQUEUE_BATCH
    assert jobs.claim_calls[0]["limit"] == CLAIM_BATCH
    assert jobs.claim_calls[0]["lease_seconds"] == LEASE_SECONDS
    assert CLAIM_BATCH < ENQUEUE_BATCH


async def test_a_declined_write_finishes_the_job_instead_of_failing_it():
    """`upsert` returning False is not an error and must not spend an attempt.

    It means the issue was archived after being queued, or its text changed
    while the model was running. Neither is the row's fault and neither is
    retried usefully: the anti-join re-queues the issue if it still needs
    embedding, so the right thing is to delete the job and report nothing
    written.
    """
    worker, jobs, embeddings = build_worker(
        claimed=[job("Deploy pipeline is flaky")],
        stored=False,
    )

    assert await worker.run_once() == 0
    assert len(jobs.completed) == 1
    assert jobs.failures == []


# ------------------------------------------------------------ the poison row


async def test_a_batch_that_fails_as_a_unit_is_retried_one_text_at_a_time():
    """THE PROPERTY THAT AIMS THE RETRY BOUND AT THE RIGHT ROW.

    A real model fails for a whole batch when one string inside it is
    pathological. Charging the batch would retire every healthy issue in it, so
    the worker re-embeds singly and only the offending text is charged.

    The call log is asserted, not only the outcome: the batch has to be TRIED
    first, or the per-call overhead `Embedder.embed`'s sequence argument exists
    to amortise is thrown away on every pass.
    """
    embedder = BreakingEmbedder()
    worker, jobs, embeddings = build_worker(
        claimed=[job("healthy one"), job(POISON), job("healthy two")],
        embedder=embedder,
    )

    assert await worker.run_once() == 2

    assert embedder.calls == [3, 1, 1, 1]
    assert len(jobs.completed) == 2
    assert len(jobs.failures) == 1


async def test_a_failure_records_the_bound_and_the_next_delay():
    embedder = BreakingEmbedder()
    worker, jobs, _ = build_worker(
        claimed=[job(POISON, attempts=2)],
        embedder=embedder,
    )

    await worker.run_once()

    failure = jobs.failures[0]

    assert failure["max_attempts"] == MAX_ATTEMPTS
    assert failure["retry_after_seconds"] == retry_delay_seconds(3)


async def test_a_failure_records_a_message_and_not_a_traceback():
    """`last_error` is bounded at 500 characters and read by an operator.

    It is also the one field in this feature that holds an exception's own text,
    which is why nothing in the GraphQL schema selects it -- see
    `IndexingState`, which is what a client gets instead.
    """
    embedder = BreakingEmbedder()
    worker, jobs, _ = build_worker(claimed=[job(POISON)], embedder=embedder)

    await worker.run_once()

    error = jobs.failures[0]["error"]

    assert "cannot tokenise this" in error
    assert "\n" not in error


@pytest.mark.parametrize("attempts", range(1, MAX_ATTEMPTS + 3))
def test_the_delay_is_never_negative_or_zero(attempts: int):
    """A zero delay would make a bounded retry a tight loop until the bound."""
    assert retry_delay_seconds(attempts) > 0


# ------------------------------------------------------------- the state


def test_a_disabled_index_reports_zeroes_a_client_cannot_misread():
    """Zero counts beside `enabled: false` must not read as a full index.

    They cannot, because the flag is what a client checks first -- but the pair
    is asserted here so that a future change making `enabled` derived from the
    counts fails rather than inverting the meaning of an empty workspace.
    """
    state = IndexingState(indexed=0, pending=0, failed=0, enabled=False)

    assert state.enabled is False
    assert (state.indexed, state.pending, state.failed) == (0, 0, 0)
