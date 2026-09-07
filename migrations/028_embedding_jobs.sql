-- The embedding lifecycle: what makes semantic search work without anybody
-- remembering to refresh it.
--
-- migrations/025_semantic_search.sql builds the whole read path -- the
-- `vector` extension, `issue_embeddings`, the generated
-- `issues.embedding_source_digest` that makes staleness a join predicate, and
-- the covering index the anti-join uses. It is correct and it is tenant-safe,
-- and on a fresh install it returns nothing at all, because nothing writes
-- `issue_embeddings`. The one thing that does -- `embeddingsRefresh` -- is a
-- GraphQL mutation somebody has to call. Nobody calls it. So a workspace's
-- duplicate suggestions say "no duplicates found" when the truth is "no
-- index", and those are not the same sentence.
--
-- This file adds the missing half: a record of WORK ATTEMPTED, so that a
-- background worker can drain the backlog, retry what failed, give up on what
-- keeps failing, and let two processes do it at once without doing anything
-- twice.
--
-- ------------------------------------------------------------------
-- THIS TABLE IS NOT THE QUEUE. IT IS THE ATTEMPT LOG.
-- ------------------------------------------------------------------
--
-- 025 argues at length against a queue table, and that argument is still
-- correct and is not overturned here:
--
--     "The regeneration queue falls out of the same equality rather than
--      being a second table: an issue needing an embedding is one whose
--      digest matches no row in `issue_embeddings` [...] A queue table would
--      be a third thing to keep in step with the other two, and it could
--      itself drift."
--
-- That stays true, and this table is deliberately built so that it cannot be
-- the thing that drifts. WHAT NEEDS EMBEDDING is still the LEFT JOIN over
-- (workspace_id, issue_id, model, source_digest) with `IS NULL` in the WHERE
-- -- see `EmbeddingRepository.list_stale`, which is unchanged, and
-- `EmbeddingJobRepository.enqueue_stale`, which inserts FROM exactly that
-- anti-join. No writer marks a row dirty here; no writer can forget to.
--
-- What this table holds is the part the anti-join genuinely cannot express,
-- because it is not a fact about the issue's text at all:
--
--   * WHO IS WORKING ON IT RIGHT NOW. The model runs with no connection held
--     -- for the reason `SearchService.refresh_embeddings` gives -- so two
--     workers reading the same anti-join would embed the same hundred issues
--     simultaneously and each pay full model time for work the other was
--     already doing. `next_attempt_at` is the lease that stops that, and
--     `FOR UPDATE SKIP LOCKED` on the claim is what makes two workers'
--     claims disjoint in the same instant.
--
--   * HOW MANY TIMES IT HAS FAILED. An issue whose text makes the embedder
--     raise -- a tokeniser bug, a pathological input, a model that only
--     falls over on one string -- is retried forever by an anti-join, which
--     has no memory. `attempts` is that memory, and it is the whole reason a
--     poison row stops instead of consuming every sweep in the installation.
--
--   * WHEN TO TRY AGAIN. Backoff, so a transient fault is retried soon and a
--     permanent one stops costing anything.
--
-- The consequence of splitting it that way is the property that matters: THE
-- ATTEMPT LOG CAN ONLY EVER CAUSE LESS WORK THAN THE TRUTH, NEVER WRONG WORK.
-- A row here that has gone obsolete cannot produce a bad embedding, because
-- the write it leads to is `EmbeddingRepository.upsert`, which re-checks the
-- digest, the workspace and `archived_at` in its own WHERE clause and
-- declines otherwise. And a row LOST here cannot lose the work, because the
-- anti-join re-enqueues it on the next sweep. Drift is therefore not a
-- correctness question in either direction; it is at worst a delay.
--
-- ------------------------------------------------------------------
-- What this file does NOT add
-- ------------------------------------------------------------------
--
-- No `status` column, and that is deliberate rather than an omission. A
-- status enum ('queued', 'running', 'failed') would be a second spelling of
-- facts two other columns already carry: "running" is `next_attempt_at` in
-- the future because a worker leased it, and "failed" is `attempts` having
-- reached the bound. Two spellings of one state are two things that disagree
-- the first time a worker dies between the two UPDATEs -- and the row that
-- says 'running' with no process behind it is exactly the row a queue never
-- recovers on its own.
--
-- No `locked_by` / `worker_id`. Nothing reads it. A lease that expires is
-- recovered by time regardless of who took it, and an identifier for the
-- taker would be a column maintained for a diagnostic nobody has asked for.
--
-- No separate `embedding_job_failures` history table. `last_error` holds the
-- most recent reason, which is what an operator reads; a full history of
-- every attempt at every issue is a table that grows without bound to answer
-- a question ("why did this fail four sweeps ago") the logs already answer.
--
-- DEPENDS ON 002, for `workspace_id` on `issues`; on 006, for
-- `issues.archived_at`; on 007, for `issues_workspace_id_key` -- the
-- UNIQUE (workspace_id, id) the composite foreign key below references; and
-- on 025, for `issues.embedding_source_digest`, whose exact output shape the
-- format check below restates. Applying this before any of them fails on a
-- column or a key that does not exist and, inside the single transaction the
-- runner wraps the file in, leaves nothing behind. That failure IS the
-- dependency check.
--
-- ------------------------------------------------------------------
-- What drains it
-- ------------------------------------------------------------------
--
-- `app.services.embedding_jobs.EmbeddingWorker`, started as a task on the
-- application's own lifespan when `EMBEDDING_WORKER_ENABLED` is set -- see
-- `app.main._start_embedding_worker`. There is no cron container and no queue
-- broker, deliberately: both are infrastructure a deployment has to operate,
-- bought to call a coroutine every thirty seconds.
--
-- Setting that flag on SEVERAL processes is safe and is the point of the claim
-- below: N workers split the backlog rather than duplicating it. A deployment
-- that sets it on none still works -- `embeddingsRefresh` sweeps one workspace
-- on demand, and `embeddingIndexingState` reports the truth either way.
--
-- Apply this ONLY through `python -m scripts.apply_migration
-- migrations/028_embedding_jobs.sql`.


CREATE TABLE embedding_jobs (
    -- No surrogate id: the row IS its key, exactly as `issue_embeddings` in
    -- 025 and `initiative_projects` in 022. One issue has at most one
    -- outstanding attempt -- a second would be two workers' worth of model
    -- time spent to store one vector -- so an `id UUID PRIMARY KEY` would
    -- need a UNIQUE (workspace_id, issue_id) beside it to say so, which is a
    -- second key that buys nothing.
    --
    -- `workspace_id` leads for the reason it leads everywhere in this schema,
    -- and here it is also what makes the per-workspace status read -- "how
    -- many of MY issues are waiting" -- an index seek rather than a scan of
    -- every tenant's backlog.
    workspace_id UUID NOT NULL,
    issue_id UUID NOT NULL,

    -- The value `issues.embedding_source_digest` had when this attempt was
    -- queued.
    --
    -- NOT the freshness mechanism -- 025's digest equality is, and this
    -- column does not participate in any read that decides whether an
    -- embedding is current. It answers one narrower question: is this
    -- outstanding attempt about the text the issue has NOW?
    --
    -- Two things turn on the answer, and both are about not wasting or losing
    -- work rather than about correctness:
    --
    --   * the enqueue sweep skips an issue that already has an attempt row at
    --     the current digest, so a queue that is deep does not spend its
    --     whole insert budget re-selecting rows it queued last cycle;
    --   * an issue EDITED while an attempt was outstanding -- or after that
    --     attempt had exhausted its retries -- is re-queued with `attempts`
    --     reset to zero. That is the right reset: what failed was the old
    --     text, and refusing to try the new text because the old one was
    --     poison would be punishing an issue for its own history.
    --
    -- The claim statement deliberately does NOT compare this against the
    -- issue's current digest. It returns the LIVE digest instead, so a worker
    -- always embeds the text that is there now and the attempt row cannot
    -- send it to embed something out of date.
    --
    -- The format check is 025's, restated: two md5s, 64 lowercase hex
    -- characters. A value of any other shape could only have come from
    -- somewhere other than the generated column, and would compare unequal
    -- forever -- presenting as an issue that is permanently re-queued with
    -- nothing reporting an error.
    source_digest TEXT NOT NULL,

    -- How many times a worker has tried and failed to embed this issue.
    --
    -- SMALLINT and not INTEGER: the application stops at a single-digit
    -- bound, the CHECK below refuses anything past 100, and two bytes is the
    -- honest width for a counter that can never reach three digits.
    --
    -- Incremented ONLY on failure. A successful attempt deletes the row --
    -- the embedding now exists, so the anti-join will not produce this issue
    -- again -- which means "how far through its retries is this" needs no
    -- companion "did it succeed" column to be read alongside it.
    --
    -- The bound itself lives in the application (see
    -- `app.domain.embedding_jobs.MAX_ATTEMPTS`) and not in this CHECK. That
    -- is on purpose: the bound is a policy that a deployment might want to
    -- change, and a policy pinned in a CHECK constraint can only be changed
    -- by a migration. What the constraint pins is the RANGE -- that this is a
    -- small counter and not somewhere to put an arbitrary integer.
    attempts SMALLINT NOT NULL DEFAULT 0,

    -- When this attempt becomes claimable again. THE ONE COLUMN THE CLAIM
    -- READS, and it carries three states rather than one flag each:
    --
    --   * in the past -- due now; a worker may take it;
    --   * in the near future -- LEASED. A worker claimed it and pushed this
    --     forward by the visibility timeout before releasing the connection
    --     to run the model. A worker that crashes mid-batch leaves rows that
    --     simply become due again, with nothing to clean up and nobody to
    --     notice, which is the property a `status = 'running'` column does
    --     not have;
    --   * 'infinity' -- retired. Set when `attempts` reaches the bound, and
    --     it is not a second spelling of "failed": it is the CONSEQUENCE of
    --     failing that many times, which is that this row is never due again.
    --     Written this way rather than filtered out by a second predicate so
    --     that a poison row sorts to the far end of the index below instead
    --     of sitting at the front of every claim being skipped.
    --
    -- DEFAULT now(), so a freshly enqueued issue is immediately claimable and
    -- the insert does not have to say so.
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Why the most recent attempt failed, for whoever is looking at a
    -- workspace whose index has stopped filling.
    --
    -- NULL until something fails, and NULL again if the row is re-queued by
    -- an edit -- so a non-NULL value always describes the attempts currently
    -- counted in `attempts`, rather than being a scar from a previous digest.
    --
    -- OPERATOR-FACING AND NEVER CLIENT-FACING. Nothing in the GraphQL schema
    -- selects this column, and nothing should: it is an exception's own text,
    -- which is precisely the "raw internal exception detail" the project's
    -- error rules forbid returning. The truthful indexing state a client CAN
    -- see is three counts and a boolean; see the status read.
    --
    -- Bounded at 500, and the writer truncates rather than relying on the
    -- constraint to refuse -- a failed embedding must not become a failed
    -- INSERT about the failed embedding.
    last_error TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT embedding_jobs_pkey PRIMARY KEY (workspace_id, issue_id),

    -- Composite, through `workspace_id`, onto `issues_workspace_id_key` from
    -- 007 -- the pattern 002 establishes and every table since restates. It
    -- is the reason there is no such thing as a cross-tenant job: there is
    -- ONE workspace_id column on this row, the primary key reads it and this
    -- foreign key reads it, so a row pairing workspace A's tenancy column
    -- with workspace B's issue does not exist to be claimed.
    --
    -- That matters more here than it looks, because the worker is the one
    -- reader in this system with no tenant of its own. It claims across every
    -- workspace at once, and the workspace it then writes an embedding for is
    -- the workspace_id it read off this row -- never one supplied by a
    -- caller. The constraint is what makes that read trustworthy.
    --
    -- ON DELETE CASCADE, and this is the one place in the embedding feature
    -- that departs from 025's RESTRICT. 025 argues for RESTRICT on
    -- `issue_embeddings` because an embedding is DATA, and a one-line delete
    -- must not discard it as an invisible side effect. An attempt row is not
    -- data: it is a note saying "somebody should embed this issue", and an
    -- issue that no longer exists is one nobody should embed. RESTRICT here
    -- would mean the first `DELETE FROM issues` this product ever grows fails
    -- on a row whose only correct handling is to vanish -- and the row would
    -- have to be deleted first by hand, in the same transaction, to satisfy a
    -- constraint protecting nothing. Same call 007 makes for `issue_labels`.
    CONSTRAINT embedding_jobs_issue_fk
        FOREIGN KEY (workspace_id, issue_id)
        REFERENCES issues (workspace_id, id)
        ON DELETE CASCADE ON UPDATE RESTRICT,

    CONSTRAINT embedding_jobs_source_digest_format
        CHECK (source_digest ~ '^[0-9a-f]{64}$'),

    CONSTRAINT embedding_jobs_attempts_range
        CHECK (attempts BETWEEN 0 AND 100),

    -- Non-empty when present. The writer wraps its truncation in `nullif`, so
    -- an exception with an empty string for a message stores NULL rather than
    -- a row that says a failure was recorded and says nothing about it.
    CONSTRAINT embedding_jobs_last_error_length
        CHECK (last_error IS NULL OR length(last_error) BETWEEN 1 AND 500)
);


-- The claim index, and the only read this table has that is not a seek on its
-- primary key.
--
-- The claim is: the oldest-due rows, across every workspace, locked with
-- SKIP LOCKED, limited to a batch. So the leading column is
-- `next_attempt_at` and NOT `workspace_id` -- which is the one place in this
-- schema where the tenant does not lead an index, and it is worth saying why
-- rather than leaving it to look like an oversight.
--
-- A background worker has no tenant. It is not serving a request and there is
-- no caller whose workspace could scope the read; its job is precisely to
-- work through every workspace's backlog. An index led by `workspace_id`
-- would make "the oldest due row anywhere" a scan of the whole table, and
-- would make the worker's cost grow with the number of workspaces rather than
-- with the amount of work outstanding.
--
-- Tenancy is not weakened by that, because nothing about this ordering
-- reaches a caller. The rows the claim returns are consumed by the worker and
-- turned into writes whose workspace_id came off the row itself; no client
-- sees the order, the count or the latency. The client-facing read -- "how
-- many of MY issues are waiting" -- is a different statement, and it uses the
-- primary key's leading `workspace_id`.
--
-- `issue_id` trails `workspace_id` to make the ordering total. Reciprocal to
-- 025's tie-break argument: without it, two rows queued in the same
-- transaction share `next_attempt_at` to the microsecond and two workers
-- claiming concurrently would have no agreed order to disagree about --
-- which is not a correctness problem under SKIP LOCKED, but does make a
-- claim's result unreproducible for anybody trying to debug one.
--
-- Not partial. `WHERE next_attempt_at < 'infinity'` would keep retired rows
-- out of the index entirely and is tempting, and it is refused because the
-- predicate would then be a second place the retirement rule lives -- and
-- because 'infinity' already sorts last, so a retired row costs one index
-- entry at the far end of a scan that stops at `now()`.
CREATE INDEX embedding_jobs_due_idx
    ON embedding_jobs (next_attempt_at, workspace_id, issue_id);


-- There is deliberately no index on `attempts`. The one read that filters on
-- it is the per-workspace count of retired rows, which is already restricted
-- to one workspace by the primary key's leading column -- so it walks that
-- workspace's own rows, which is the same set it would have to count anyway.
--
-- And no index for the foreign key's referencing side: it is
-- `embedding_jobs_pkey`'s own (workspace_id, issue_id), so the primary key IS
-- that index, exactly as in 025.
