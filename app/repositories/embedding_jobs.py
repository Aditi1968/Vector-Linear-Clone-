from uuid import UUID

import asyncpg

from app.domain.embedding_jobs import ClaimedEmbeddingJob, IndexingState
from app.domain.semantic_search import EmbeddingSource
from app.domain.tenancy import WorkspaceScope


class EmbeddingJobRepository:
    """SQL over `embedding_jobs`, which is an attempt log and not a queue.

    WHAT NEEDS EMBEDDING is still the anti-join migration 025 built -- a live
    issue with no row in `issue_embeddings` matching it on all four of
    (workspace, issue, model, digest). `enqueue_stale` below INSERTs from
    exactly that anti-join, and `EmbeddingRepository.list_stale` still answers
    it unchanged for the per-workspace mutation. No writer marks a row dirty
    here, so no writer can forget to.

    What this table adds is the part the anti-join cannot express, because it is
    not a fact about the issue's text: who is working on a row right now, how
    many times it has failed, and when to try again. See
    migrations/028_embedding_jobs.sql for the argument in full, and for why the
    consequence is that this table can only ever cause LESS work than the truth,
    never wrong work.

    TENANCY, AND WHY THIS FILE LOOKS DIFFERENT FROM EVERY OTHER REPOSITORY.
    Two of these statements carry no `workspace_id = $1` predicate at all, and
    that is deliberate rather than an omission: `enqueue_stale` and `claim` are
    issued by a background worker, which is the one reader in this system with
    no tenant. It is not serving a request, no caller's membership could scope
    it, and its whole job is to work through every workspace's backlog.

    That does not weaken isolation, because nothing either statement returns
    reaches a client. The rows `claim` hands back are consumed by the worker and
    turned into `EmbeddingRepository.upsert` calls whose scope is built from the
    `workspace_id` READ OFF THE ROW -- never one supplied by a caller, and never
    an ambient default. The composite foreign key in 028 is what makes that read
    trustworthy: there is one `workspace_id` column on the row, the primary key
    reads it and the foreign key reads it, so a row pairing workspace A's
    tenancy column with workspace B's issue does not exist to be claimed.

    The one CLIENT-facing statement here is `indexing_state`, and it is scoped
    the way every client-facing read in this schema is: `workspace_id = $1` in
    the WHERE clause, on the primary key's leading column.

    Connections are passed in and never acquired. `asyncpg.Record` does not
    leave this class.
    """

    async def enqueue_stale(
        self,
        connection: asyncpg.Connection,
        *,
        model: str,
        limit: int,
    ) -> int:
        """Queue up to `limit` issues, anywhere, that have no current embedding.

        Returns how many rows became due. Zero means the installation is fully
        indexed under `model`, which is what a worker loops until.

        THE SELECT IS 025'S ANTI-JOIN, unchanged in every respect that decides
        what is stale: the LEFT JOIN onto `issue_embeddings` on all four of
        (workspace, issue, model, digest), `IS NULL` in the WHERE, and
        `archived_at IS NULL` beside it. An archived issue therefore never
        enqueues, and an issue whose title was edited by a bulk import, a
        webhook or a hand-run UPDATE appears here on the next sweep because
        `issues.embedding_source_digest` changed in the same statement that
        changed the title.

        THE SECOND LEFT JOIN -- onto this table, at the CURRENT digest -- is
        what makes the sweep make progress, and it is a correctness matter
        rather than a saving. Without it the `limit` budget is spent re-selecting
        rows that already have an attempt row, and a workspace holding `limit`
        retired poison rows would starve every issue filed after them: the sweep
        would return the same exhausted rows forever and never reach a new one.

        ON CONFLICT then only ever fires for an issue whose queued attempt is
        about text it no longer has, and the reset is the right one: `attempts`
        back to zero and `last_error` back to NULL, because what failed was the
        OLD text and refusing to try the new text would be punishing an issue
        for its own history. The `WHERE` on the DO UPDATE keeps that reset off a
        row another worker inserted at this same digest a moment ago, which is
        the one way two concurrent sweeps could otherwise discard each other's
        attempt counts.

        Newest first, matching `issues_workspace_live_created_at_id_idx` from
        migration 006 -- and it is the right product answer as well as the
        cheaper walk: a freshly filed issue is the one somebody is about to
        search for a duplicate of.

        THE CEILING, stated rather than discovered. This is an anti-join, so
        once every issue is either embedded or already queued the scan walks the
        whole workspace to find nothing: work bounded per sweep, unbounded per
        row skipped. `EmbeddingRepository.list_stale` carries the same ceiling
        and it matters more here, because this runs every IDLE_POLL_SECONDS
        forever rather than when somebody asks. It is still a walk of index
        pages -- both joins are covered -- so the number to watch is issues per
        installation, not bytes.

        The upgrade path, so this is a decision with an exit: a watermark. Carry
        `max(issues.updated_at)` seen by the last sweep and add
        `issues.updated_at > $n`, which turns the scan into a range on an index
        the schema already has. Not done here, because it is a second piece of
        state to keep in step with the digest -- exactly the drift 025 declined
        a queue table to avoid -- and it should be bought on the day a sweep
        shows up in a profile, not before.
        """
        queued = await connection.fetchval(
            """
            WITH stale AS (
                SELECT
                    issues.workspace_id,
                    issues.id AS issue_id,
                    issues.embedding_source_digest AS source_digest
                FROM issues
                LEFT JOIN issue_embeddings
                    ON issue_embeddings.workspace_id = issues.workspace_id
                    AND issue_embeddings.issue_id = issues.id
                    AND issue_embeddings.model = $1
                    AND issue_embeddings.source_digest
                        = issues.embedding_source_digest
                LEFT JOIN embedding_jobs
                    ON embedding_jobs.workspace_id = issues.workspace_id
                    AND embedding_jobs.issue_id = issues.id
                    AND embedding_jobs.source_digest
                        = issues.embedding_source_digest
                WHERE issues.archived_at IS NULL
                    AND issue_embeddings.issue_id IS NULL
                    AND embedding_jobs.issue_id IS NULL
                ORDER BY issues.created_at DESC, issues.id DESC
                LIMIT $2
            ),
            enqueued AS (
                INSERT INTO embedding_jobs (workspace_id, issue_id, source_digest)
                SELECT workspace_id, issue_id, source_digest
                FROM stale
                ON CONFLICT ON CONSTRAINT embedding_jobs_pkey DO UPDATE
                SET source_digest = EXCLUDED.source_digest,
                    attempts = 0,
                    next_attempt_at = now(),
                    last_error = NULL,
                    updated_at = now()
                WHERE embedding_jobs.source_digest <> EXCLUDED.source_digest
                RETURNING 1
            )
            SELECT count(*) FROM enqueued
            """,
            model,
            limit,
        )

        return int(queued)

    async def claim(
        self,
        connection: asyncpg.Connection,
        *,
        limit: int,
        lease_seconds: float,
    ) -> list[ClaimedEmbeddingJob]:
        """Lease up to `limit` due jobs, and hand back the text to embed.

        THE STATEMENT THAT MAKES TWO WORKERS SAFE, and the whole of it is
        `FOR UPDATE SKIP LOCKED` in the CTE. Two workers running this in the
        same instant take disjoint sets: rows the other has locked are skipped
        during the scan rather than waited on, so neither blocks and neither
        sees a row the other took. Without SKIP LOCKED the second worker would
        block on the first's lock and then claim the very same rows once it
        committed, which is the classic way a queue table processes everything
        twice.

        The lease is `next_attempt_at` pushed `lease_seconds` into the future by
        this same statement, which is why the claim is an UPDATE and not a
        SELECT. The row lock ends when this transaction commits -- a fraction of
        a second later, because the caller releases the connection before
        running the model -- so the lock is what makes the CLAIM atomic and the
        lease is what keeps the row invisible for as long as the WORK takes.
        A worker that dies mid-batch leaves rows that simply become due again,
        with nothing to clean up and nobody to notice.

        THE TEXT RETURNED IS THE ISSUE'S LIVE TEXT, not anything reconstructed
        from the queue row. `embedding_jobs.source_digest` records what the text
        looked like when the job was queued and is deliberately not compared
        here: a worker must embed what the issue says NOW, and the digest it
        writes alongside the vector has to be the one the text it actually
        embedded produced. `EmbeddingRepository.upsert` re-checks that digest
        against the server's own copy and declines if it moved again in the
        meantime, so an edit during the model run costs a wasted vector rather
        than a wrong one.

        Archived issues are NOT filtered out. A job for an issue archived after
        it was queued is claimed, embedded and then declined by `upsert`'s own
        `archived_at IS NULL` guard, and the worker deletes it -- see
        `EmbeddingWorker._store`. Filtering here instead would leave that row
        permanently due and permanently unclaimable, which is a spin rather than
        a saving.

        `ORDER BY next_attempt_at, workspace_id, issue_id` matches
        `embedding_jobs_due_idx` exactly, so this is a walk of the index that
        stops at `now()` rather than a sort of the table. The tie-break makes
        the order total: rows queued in one transaction share `next_attempt_at`
        to the microsecond, which under SKIP LOCKED is not a correctness problem
        but does make a claim unreproducible for anybody debugging one.
        """
        rows = await connection.fetch(
            """
            WITH due AS (
                SELECT workspace_id, issue_id
                FROM embedding_jobs
                WHERE next_attempt_at <= now()
                ORDER BY next_attempt_at, workspace_id, issue_id
                LIMIT $1
                FOR UPDATE SKIP LOCKED
            )
            UPDATE embedding_jobs
            SET next_attempt_at = now() + make_interval(secs => $2::float8),
                updated_at = now()
            FROM due
            JOIN issues
                ON issues.workspace_id = due.workspace_id
                AND issues.id = due.issue_id
            WHERE embedding_jobs.workspace_id = due.workspace_id
                AND embedding_jobs.issue_id = due.issue_id
            RETURNING
                embedding_jobs.workspace_id,
                embedding_jobs.issue_id,
                embedding_jobs.attempts,
                issues.embedding_source_digest,
                issues.title,
                issues.description
            """,
            limit,
            lease_seconds,
        )

        return [
            ClaimedEmbeddingJob(
                workspace_id=row["workspace_id"],
                attempts=row["attempts"],
                source=EmbeddingSource(
                    issue_id=row["issue_id"],
                    source_digest=row["embedding_source_digest"],
                    title=row["title"],
                    description=row["description"],
                ),
            )
            for row in rows
        ]

    async def complete(
        self,
        connection: asyncpg.Connection,
        *,
        workspace_id: UUID,
        issue_id: UUID,
    ) -> None:
        """Forget this attempt, because there is nothing left to attempt.

        A DELETE and not a `status = 'done'`, which is the reason 028 has no
        status column at all: the embedding now exists, so 025's anti-join will
        not produce this issue again, and a row saying "done" would be a second
        spelling of a fact `issue_embeddings` already holds -- one that could
        disagree with it, and that would accumulate one row per issue forever.

        `workspace_id` is in the WHERE clause even though `issue_id` alone would
        be unique. It is the primary key's leading column, so this is a seek
        rather than an index scan -- and it means a caller that somehow held the
        wrong workspace could not delete another tenant's row by id.
        """
        await connection.execute(
            """
            DELETE FROM embedding_jobs
            WHERE workspace_id = $1 AND issue_id = $2
            """,
            workspace_id,
            issue_id,
        )

    async def fail(
        self,
        connection: asyncpg.Connection,
        *,
        workspace_id: UUID,
        issue_id: UUID,
        error: str,
        max_attempts: int,
        retry_after_seconds: float,
    ) -> None:
        """Record that an attempt failed, and decide whether there will be more.

        `attempts + 1` is computed BY THE SERVER rather than passed in. The
        lease already guarantees one worker per row, so a passed-in absolute
        value would agree -- but the increment is the memory that stops a poison
        row, and the row's own count is the only value that cannot be stale.

        THE RETIREMENT IS 'infinity', not a status. A row that has failed
        `max_attempts` times is never due again, which is exactly what an
        infinitely distant `next_attempt_at` says, and saying it that way keeps
        the claim above a single predicate over a single index. Written into the
        ordering rather than filtered out of it so that a retired row sorts to
        the far end of `embedding_jobs_due_idx` instead of sitting at its front
        being skipped by every claim.

        `max_attempts` comes from `app.domain.embedding_jobs.MAX_ATTEMPTS` and
        not from a CHECK constraint, so a deployment can change the bound
        without a migration; 028 pins only the RANGE.

        `left(..., 500)` and `nullif` together are what stop a failed embedding
        becoming a failed INSERT about the failed embedding: the truncation
        satisfies `embedding_jobs_last_error_length` rather than relying on the
        constraint to refuse, and an exception whose message is empty stores
        NULL rather than a row that records a failure and says nothing about it.

        `last_error` is OPERATOR-FACING AND NEVER CLIENT-FACING. It is an
        exception's own text, which is precisely the raw internal detail this
        project's error rules keep out of responses; nothing in the GraphQL
        schema selects this column. What a client can see is `indexing_state`.
        """
        await connection.execute(
            """
            UPDATE embedding_jobs
            SET attempts = attempts + 1,
                last_error = nullif(left($3::text, 500), ''),
                next_attempt_at = CASE
                    WHEN attempts + 1 >= $4::int THEN 'infinity'::timestamptz
                    ELSE now() + make_interval(secs => $5::float8)
                END,
                updated_at = now()
            WHERE workspace_id = $1 AND issue_id = $2
            """,
            workspace_id,
            issue_id,
            error,
            max_attempts,
            retry_after_seconds,
        )

    async def indexing_state(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        model: str,
    ) -> IndexingState:
        """How much of this workspace's semantic index exists, in three counts.

        THE READ THAT LETS A CLIENT SAY "index building" INSTEAD OF "no
        duplicates". Those are different sentences and before this statement
        existed nothing could tell them apart: `issueDuplicateSuggestions`
        answers with an empty list both when a workspace has no similar issues
        and when it has no embeddings at all, which on a fresh install is
        always.

        The three counts PARTITION this workspace's live issues -- every issue
        falls in exactly one, and they sum to the workspace's live issue count.
        That is what makes them readable as a progress bar rather than three
        unrelated numbers.

        `indexed` is defined by 025's freshness equality and by nothing else: an
        embedding under this model whose `source_digest` still matches the
        issue's. So it counts what semantic search can ACTUALLY see, not what
        has ever been written -- an issue whose title was edited a second ago
        drops straight back out of it, which is the honest answer because the
        stored vector describes text that no longer exists.

        `failed` is carved out of the remainder by this table, and the job join
        carries the digest for the same reason: a job that exhausted its retries
        against the OLD text says nothing about the new text, so an edited issue
        counts as pending again rather than staying failed until the next sweep
        resets it.

        Tenant-scoped on `issues.workspace_id`, which is the leading column of
        every index involved. Note that this is a COUNT, and a count is exactly
        the kind of read a cross-tenant defect hides in -- a filter applied
        after an aggregate would report another workspace's issues as this one's
        progress. There is no aggregate here that is not already inside the
        scope.
        """
        row = await connection.fetchrow(
            """
            SELECT
                count(*) FILTER (
                    WHERE issue_embeddings.issue_id IS NOT NULL
                ) AS indexed,
                count(*) FILTER (
                    WHERE issue_embeddings.issue_id IS NULL
                        AND embedding_jobs.next_attempt_at
                            IS DISTINCT FROM 'infinity'::timestamptz
                ) AS pending,
                count(*) FILTER (
                    WHERE issue_embeddings.issue_id IS NULL
                        AND embedding_jobs.next_attempt_at
                            = 'infinity'::timestamptz
                ) AS failed
            FROM issues
            LEFT JOIN issue_embeddings
                ON issue_embeddings.workspace_id = issues.workspace_id
                AND issue_embeddings.issue_id = issues.id
                AND issue_embeddings.model = $2
                AND issue_embeddings.source_digest
                    = issues.embedding_source_digest
            LEFT JOIN embedding_jobs
                ON embedding_jobs.workspace_id = issues.workspace_id
                AND embedding_jobs.issue_id = issues.id
                AND embedding_jobs.source_digest
                    = issues.embedding_source_digest
            WHERE issues.workspace_id = $1
                AND issues.archived_at IS NULL
            """,
            scope.workspace_id,
            model,
        )

        # `row` is never None: these are aggregates with no GROUP BY, so a
        # workspace with no issues at all still produces one row of zeroes.
        # That assertion is the reason this is not written defensively.
        assert row is not None

        return IndexingState(
            indexed=row["indexed"],
            pending=row["pending"],
            failed=row["failed"],
            # True by construction rather than by inspection: this statement
            # needs a model NAME, and the only thing that has one is an embedder
            # that was wired. A caller with none never reaches here -- see
            # `SearchService.indexing_state`, which answers without a round trip.
            enabled=True,
        )
