import asyncio

import asyncpg
import structlog

from app.domain.embedding_jobs import (
    LEASE_SECONDS,
    MAX_ATTEMPTS,
    ClaimedEmbeddingJob,
    retry_delay_seconds,
)
from app.domain.tenancy import WorkspaceScope
from app.repositories.embedding_jobs import EmbeddingJobRepository
from app.repositories.embeddings import EmbeddingRepository
from app.services.embeddings import Embedder, vector_literal


logger = structlog.get_logger(__name__)


# How many stale issues one pass may queue, across the whole installation.
#
# The enqueue is an anti-join, so its cost grows with issues walked rather than
# issues queued -- see `EmbeddingRepository.list_stale` on that ceiling. A large
# number here would make a single pass over a fully indexed installation
# expensive to discover it has nothing to do; a small one would take a hundred
# passes to fill a new deployment's index. 500 is one comfortable statement.
ENQUEUE_BATCH = 500

# How many jobs one pass leases and embeds.
#
# Deliberately much smaller than the enqueue budget, because this is the number
# that decides how long a pass HOLDS work: every claimed job is invisible to
# every other worker for LEASE_SECONDS, so a worker that claims 500 and then
# dies has parked 500 issues for five minutes. 50 is a batch a real sentence
# encoder embeds in well under a second and a crash forfeits little.
CLAIM_BATCH = 50

# How long a worker waits after a pass that found nothing.
#
# The pass that finds nothing is the overwhelmingly common one -- a steady-state
# installation is fully indexed and this is a poll -- so this number is
# essentially the worker's whole cost when there is no work. Thirty seconds is
# also the longest a freshly filed issue waits to become semantically findable,
# which is the product-visible half of the same trade.
#
# A pass that found work loops again immediately rather than sleeping: a
# deployment filling a new index should drain it as fast as the model runs, not
# 50 issues every thirty seconds.
IDLE_POLL_SECONDS = 30.0

# How long the loop waits after a pass raised.
#
# Longer than the idle poll, because the failures that reach the loop rather
# than a single job are the ones a retry cannot fix quickly: the database is
# unreachable, the pool is exhausted. Backing off further keeps a worker from
# turning an outage into a tight reconnect loop against a server already in
# trouble.
ERROR_BACKOFF_SECONDS = 60.0


class EmbeddingWorker:
    """The background pass that fills `issue_embeddings` in, and keeps it filled.

    THE GAP THIS CLOSES. Migration 025 builds the entire read path for semantic
    search and it is correct, and on a fresh install it returns nothing at all,
    because nothing writes `issue_embeddings`. The one thing that did --
    `embeddingsRefresh` -- is a GraphQL mutation somebody has to call, and
    nobody calls it. So a workspace's duplicate suggestions said "no duplicates
    found" when the truth was "no index", and those are not the same sentence.

    Four lifecycle events, and all four are handled by ONE mechanism rather than
    four hooks, which is the entire design:

      * a NEW issue has no matching row in `issue_embeddings`, so the anti-join
        produces it;
      * an EDITED title or description changes `issues.embedding_source_digest`
        in the same statement that changed the text, so the stored vector stops
        matching and the anti-join produces it again;
      * an ARCHIVED issue is excluded by `archived_at IS NULL`, so it stops
        being produced -- and if a job was already queued, `upsert` declines it
        and the job is deleted;
      * a DELETED issue takes its job row with it, through the composite foreign
        key's ON DELETE CASCADE.

    Nothing anywhere marks a row dirty, so nothing anywhere can forget to. The
    bulk import path, the template apply, the GitHub webhook and the Slack
    command all get correct behaviour without knowing this class exists.

    NO EMBEDDER MEANS NO WORK, and that is a wire rather than a try/except, for
    the reason `SearchService` gives about the same decision: a deployment
    without a model wires None and every pass is honestly a no-op, instead of
    the answer to "does this deployment index issues" varying with what the last
    pass happened to hit.

    Transactions are owned here and connections are passed down. The three
    phases are deliberately three acquisitions with the model run between them
    -- see `run_once`.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        jobs: EmbeddingJobRepository,
        embeddings: EmbeddingRepository,
        embedder: Embedder | None,
    ):
        self._pool = pool
        self._jobs = jobs
        self._embeddings = embeddings
        self._embedder = embedder

    async def run_once(self) -> int:
        """One pass: queue what is stale, embed a batch, report what was written.

        Returns how many embeddings reached the database, which is not always
        how many jobs were claimed: an issue edited between the claim and the
        write declines its own row, and a job that failed writes an attempt
        instead of a vector. A caller loops until this reports zero.

        THREE PHASES, AND THE CONNECTION IS RELEASED BETWEEN THEM. Queue, then
        claim, then run the model with NO connection held, then write. A real
        sentence encoder is hundreds of milliseconds for a batch and this
        process shares its pool with the request path; holding a slot across the
        model would let one background pass starve every search in flight. It is
        the same argument `SearchService.refresh_embeddings` makes, and it
        matters more here because this runs forever.

        The claim commits on its own, before the model runs. That is what makes
        the lease a lease: the row lock ends in milliseconds and
        `next_attempt_at` is what keeps the row invisible for the length of the
        work.

        ONE TRANSACTION PER JOB in the write phase, unlike
        `SearchService.refresh_embeddings`, which wraps its whole batch in one.
        The difference is what a failure means. There, a partial batch under a
        sweep that reported nothing is the bad outcome. Here every job is
        independent and already has its own attempt counter, so rolling back
        forty-nine good embeddings because the fiftieth issue was deleted a
        moment ago would throw away work and re-run a model for no reason.
        """
        embedder = self._embedder

        if embedder is None:
            return 0

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await self._jobs.enqueue_stale(
                    connection,
                    model=embedder.name,
                    limit=ENQUEUE_BATCH,
                )

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                claimed = await self._jobs.claim(
                    connection,
                    limit=CLAIM_BATCH,
                    lease_seconds=LEASE_SECONDS,
                )

        if not claimed:
            return 0

        results = self._embed(embedder, claimed)
        written = 0

        async with self._pool.acquire() as connection:
            # `strict=True`: `_embed` returns one result per job by
            # construction, and a mismatch would otherwise silently truncate to
            # the shorter list -- which presents as a pass that quietly stops
            # embedding rows.
            for job, result in zip(claimed, results, strict=True):
                async with connection.transaction():
                    written += await self._store(
                        connection,
                        model=embedder.name,
                        job=job,
                        result=result,
                    )

        return written

    async def run_forever(self) -> None:
        """Pass after pass, until the task is cancelled.

        THE WHOLE SCHEDULER, and it is a loop and a sleep on purpose. This
        repository has no cron, no Celery and no queue broker, and adding one to
        run a function every thirty seconds would be a deployment dependency
        bought for a `while True`. See `app.main.lifespan` for how it is
        started and `Settings.embedding_worker_enabled` for what turns it on.

        `except Exception` is deliberate and is the one place in this codebase
        that catches broadly. A supervisor loop that propagates has no
        supervisor above it: the task ends, nothing restarts it, and the index
        silently stops being maintained for the life of the process -- which is
        the exact failure this whole class exists to end. So a pass that raises
        is logged and retried after a longer pause, and the pass itself remains
        as specific as any other code here.

        `asyncio.CancelledError` derives from BaseException and so is NOT caught
        by that clause. That is what makes shutdown work: the lifespan cancels
        this task, the cancellation travels out of `asyncio.sleep`, and the loop
        ends rather than logging its own shutdown as a failure and going round
        again.
        """
        while True:
            try:
                written = await self.run_once()
            except Exception:
                logger.exception("embedding_worker.pass_failed")

                await asyncio.sleep(ERROR_BACKOFF_SECONDS)
            else:
                if written == 0:
                    await asyncio.sleep(IDLE_POLL_SECONDS)

    def _embed(
        self,
        embedder: Embedder,
        jobs: list[ClaimedEmbeddingJob],
    ) -> list[list[float] | str]:
        """One vector per job, or the reason that job could not have one.

        A `str` in the list is an error message and a `list[float]` is a vector.
        A union rather than a parallel list of errors, because the two are the
        same slot: exactly one of them is true per job, and two lists would be
        two things that could get out of step by one.

        THE BATCH IS TRIED FIRST because a real model's cost is dominated by
        per-call overhead -- that is why `Embedder.embed` takes a sequence at
        all. THE FALLBACK EXISTS because a batch fails as a unit: one
        pathological string makes `embed` raise for all fifty, and charging all
        fifty an attempt would retire forty-nine healthy issues for a defect in
        the fiftieth. Re-embedding one at a time after a batch failure is what
        keeps the retry bound aimed at the row that actually earns it.

        The exception's text is kept and the exception is not re-raised, because
        this is a worker draining a queue: a poison row has to be RECORDED and
        BOUNDED, not allowed to end the pass. Catching `Exception` here is
        deliberate for that reason and for one more -- the embedder may be a
        third-party model this repository does not own, so the set of exceptions
        it can raise is not a set this code could enumerate honestly.
        """
        texts = [job.source.text for job in jobs]

        try:
            return list(embedder.embed(texts))
        except Exception:
            logger.warning(
                "embedding_worker.batch_failed",
                batch=len(texts),
                exc_info=True,
            )

        results: list[list[float] | str] = []

        for text in texts:
            try:
                results.append(embedder.embed([text])[0])
            except Exception as exc:
                # `str(exc)` and not the traceback: this string is written to
                # `embedding_jobs.last_error`, which is bounded at 500
                # characters and read by an operator wondering why a workspace
                # stopped indexing. The traceback is already in the log line
                # above.
                results.append(f"{type(exc).__name__}: {exc}")

        return results

    async def _store(
        self,
        connection: asyncpg.Connection,
        *,
        model: str,
        job: ClaimedEmbeddingJob,
        result: list[float] | str,
    ) -> int:
        """Write one job's outcome, and return whether an embedding came of it.

        THE SCOPE IS BUILT FROM THE CLAIMED ROW and from nothing else. A worker
        has no viewer and no membership, so there is no `AuthorizedWorkspaceScope`
        to be had and constructing one would be a lie about what was checked.
        What makes this bare `WorkspaceScope` trustworthy is two things that are
        both in the schema rather than here: the composite foreign key in
        migration 028, which means a job row cannot pair one workspace's tenancy
        column with another's issue, and `EmbeddingRepository.upsert`, whose own
        WHERE clause re-checks `issues.workspace_id = $1 AND issues.id = $2` and
        writes nothing if they do not agree.

        A DECLINED WRITE DELETES THE JOB, and that is right in both of the ways
        it can happen. The issue was archived after being queued -- nobody
        should embed it, and if it is ever unarchived the anti-join queues it
        again. Or its text changed while the model was running -- the vector in
        hand describes text that no longer exists, and the next enqueue sweep
        re-queues the issue at its new digest. Neither is a failure and neither
        should spend an attempt: this row's work is genuinely finished.

        A FAILED EMBEDDING records the attempt instead. `job.attempts` is the
        count before this one, so `+ 1` is the retry this failure represents and
        is what the backoff is computed from; the server does its own increment
        and its own comparison against the bound, so the two cannot disagree
        about when to give up.
        """
        if isinstance(result, str):
            await self._jobs.fail(
                connection,
                workspace_id=job.workspace_id,
                issue_id=job.source.issue_id,
                error=result,
                max_attempts=MAX_ATTEMPTS,
                retry_after_seconds=retry_delay_seconds(job.attempts + 1),
            )

            return 0

        stored = await self._embeddings.upsert(
            connection,
            scope=WorkspaceScope(workspace_id=job.workspace_id),
            issue_id=job.source.issue_id,
            model=model,
            source_digest=job.source.source_digest,
            embedding=vector_literal(result),
        )

        await self._jobs.complete(
            connection,
            workspace_id=job.workspace_id,
            issue_id=job.source.issue_id,
        )

        return 1 if stored else 0
