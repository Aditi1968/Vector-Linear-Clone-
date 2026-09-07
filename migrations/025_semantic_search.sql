-- Semantic search: the second half of the query box, and the first half of
-- "haven't we already filed this?".
--
-- migrations/011_search.sql answers "which issues contain these words". It
-- says at its own head what that costs: no typo tolerance, no synonyms beyond
-- the `english` dictionary's stemming. The gap it leaves is not typos -- it is
-- that "the build keeps dying" and "CI fails intermittently" are the same
-- issue filed twice and share no stem at all, so the lexical index scores them
-- at zero against each other and the duplicate gets filed.
--
-- This file adds the other kind of index: an embedding of the issue's text,
-- compared by direction rather than by vocabulary. It EXTENDS 011 and replaces
-- nothing. `issues.search_vector` and its GIN index stay exactly as they are,
-- every statement in IssueRepository.search still runs, and the hybrid read
-- this file exists for fuses the two orderings -- see the note on fusion at
-- the foot of this file, and app/repositories/embeddings.py for the SQL.
--
-- Three things are added, and they are one feature:
--
--   * the `vector` extension, and `issue_embeddings` -- one embedding per
--     issue, with the model that produced it recorded beside it;
--   * `issues.embedding_source_digest`, a GENERATED column holding a digest of
--     the exact text an embedding would be built from. This is the freshness
--     mechanism and the most important decision in the file; see its own note;
--   * the indexes that let a workspace read its own embeddings without reading
--     anybody else's.
--
-- Apply this ONLY through `python -m scripts.apply_migration
-- migrations/025_semantic_search.sql`. The runner wraps the file in one
-- transaction, takes an advisory lock and records a checksum; a hand-run gets
-- none of that and executes statement-at-a-time in autocommit, which for this
-- file means a table rewritten with a generated column and, if the extension
-- turns out to be unavailable on that server, an `issues` table carrying a
-- digest column with nothing that reads it.
--
-- DEPENDS ON 002, which puts `workspace_id` on `issues`; on 006, which adds
-- `issues.archived_at`; and on 007, which declares `issues_workspace_id_key`
-- -- the UNIQUE (workspace_id, id) that the composite foreign key below
-- references. Applying this file before any of them fails on a column or a
-- key that does not exist and -- inside the single transaction the runner
-- wraps the file in -- leaves nothing behind. That failure IS the dependency
-- check: the ledger records what has been applied and not what depends on
-- what, so the schema itself is what has to refuse an out-of-order apply.
--
-- ------------------------------------------------------------------
-- What this file does NOT do
-- ------------------------------------------------------------------
--
-- It does not embed projects. 011 indexes both `issues` and `projects` because
-- both are things a person searches for by name; duplicate detection is about
-- issues, a workspace has three orders of magnitude fewer projects than
-- issues, and the lexical index already finds a project by its name. A second
-- embedding table for projects is one CREATE TABLE on the day a resolver
-- actually asks for it, and until then it would be a table nothing writes.
--
-- It does not write embeddings. Nothing here computes a vector: PostgreSQL has
-- no model, so the rows in `issue_embeddings` arrive from the application --
-- see app/services/embeddings.py for what produces them and
-- app/services/search.py for when. That asymmetry with 011 (whose vector IS
-- computed by the server, and therefore cannot be stale) is the whole reason
-- the digest column below exists.


-- pgvector, for the `vector` type, the `<=>` operator and the operator classes
-- named in the index discussion below.
--
-- Guarded, for exactly the reasons migrations/008_cycles.sql gives at length
-- about btree_gist and 011 restates about btree_gin. Rule 2 of
-- tests/test_migration_lint.py bans defensive DDL because a guard reports
-- success without establishing that the existing object matches the one the
-- migration describes, and an extension has no shape for the guard to hide --
-- it is present or absent, and neither spelling pins a version. The exemption
-- is anchored to CREATE EXTENSION alone.
--
-- Bare would also be actively wrong against a managed PostgreSQL that
-- pre-installs pgvector -- Neon does, which is this project's development
-- database. `CREATE EXTENSION vector` errors on "already exists" before
-- reaching the privilege check, and the ledger never records a migration that
-- errored, so the deployment is stuck.
--
-- Unlike btree_gin and btree_gist, `vector` is NOT a trusted extension and NOT
-- part of contrib: it has to be installed on the server before any role can
-- create it, and the stock `postgres:18` image does not ship it. That is a
-- deployment fact rather than a schema one, and it is stated here because it
-- is the one way this migration can fail on a server where every other
-- migration succeeds. tests/conftest.py therefore starts
-- `pgvector/pgvector:pg18` -- which is the official `postgres:18` image with
-- pgvector built into it, same major version, same uuidv7() -- so that the db
-- suite exercises the real CREATE EXTENSION rather than skipping past it.
CREATE EXTENSION IF NOT EXISTS vector;


-- A digest of the text an embedding is built from, maintained by the server.
--
-- ------------------------------------------------------------------
-- THE FRESHNESS DECISION
-- ------------------------------------------------------------------
--
-- An embedding is derived data that the database cannot derive. Rename an
-- issue and `search_vector` is recomputed by the same statement that renamed
-- it -- 011 chose a GENERATED column precisely so that no INSERT or UPDATE
-- anywhere in this system can leave the two out of step. No such column is
-- possible here: computing the vector needs a model, so the row in
-- `issue_embeddings` is written by a separate statement at a later time, and
-- between the rename and that statement the stored vector describes text that
-- no longer exists.
--
-- Three ways to handle that were considered.
--
--   * A dirty flag -- `issue_embeddings.stale BOOLEAN` -- set by whoever
--     updates the issue. Rejected: it is only as correct as the rule that
--     every writer remembers to set it, and this schema has a bulk import
--     path, a template apply, a GitHub webhook and a Slack command that all
--     write issues. The first writer that forgets serves a stale vector as a
--     fresh one, silently and forever.
--
--   * A trigger setting that flag. Better -- it catches every writer -- but a
--     trigger needs a PL/pgSQL body, which rule 1 of tests/test_migration_lint
--     cannot read past (a function body's BEGIN is indistinguishable from
--     transaction control to a regex), and it is a second place the rule
--     lives. It can also be disabled.
--
--   * THIS: derive, in the row, a digest of the text an embedding WOULD be
--     built from, and store the digest that WAS built from beside the vector.
--     Freshness is then not a flag anyone sets and not a state anyone
--     maintains -- it is an equality between two columns, evaluated at read
--     time. A stale embedding does not match its issue, so it is not returned
--     at all; it does not have to be found, marked, or cleaned up first.
--
-- The third is what 011's argument turns into once the derived value cannot be
-- computed by the server. The database still derives what it can -- what the
-- CURRENT text is -- and the join carries the comparison, so there is no
-- INSERT or UPDATE, now or later, that can leave a vector looking fresh.
--
-- The regeneration queue falls out of the same equality rather than being a
-- second table: an issue needing an embedding is one whose digest matches no
-- row in `issue_embeddings`, which is a LEFT JOIN with the digest in the join
-- condition and `IS NULL` in the WHERE. A queue table would be a third thing
-- to keep in step with the other two, and it could itself drift.
--
-- ------------------------------------------------------------------
-- Why md5, and what that costs
-- ------------------------------------------------------------------
--
-- Two md5s concatenated, not one over the two fields joined by a separator.
-- Any separator can appear in a title, so `md5(title || sep || description)`
-- gives ("a" + sep, "b") and ("a", sep + "b") the same digest -- two different
-- issues whose embeddings would then be interchangeable. Digesting each field
-- and concatenating the results has no separator to confuse.
--
-- md5 and not sha256, and this is a limitation rather than a preference.
-- `sha256` takes bytea, the only text-to-bytea conversion in core is
-- `convert_to`, and `convert_to` is STABLE rather than IMMUTABLE -- PostgreSQL
-- refuses it in a generated column outright. `md5(text)` is IMMUTABLE and is
-- the only cryptographic-ish digest over text that is.
--
-- The ceiling that leaves, stated rather than discovered: md5 collisions are
-- constructible, so a person able to choose an issue's title could in
-- principle write one whose digest matches a previous title and make this
-- column report a stale embedding as fresh. What that buys them is a search
-- result ranked by the OLD text of their OWN issue, in their OWN workspace --
-- tenancy is enforced by `workspace_id` in the WHERE clause and is not
-- affected by any of this. It is a relevance defect, not a disclosure. Upgrade
-- path if that ever stops being true: an IMMUTABLE SQL wrapper around
-- `sha256(convert_to(...))`, which is a function this project would then have
-- to own -- and rule 1 of the lint suite has to grow its dollar-quoting
-- exemption first.
--
-- No NOT NULL, and none is needed: `coalesce` makes the expression total, so
-- the constraint would restate the expression rather than constrain it. Rule 4
-- of the linter refuses it anyway, and correctly.
--
-- This ALTER rewrites the table, exactly as 011's two did and for the same
-- reason: a stored generated column has to be computed for every existing row,
-- so it takes ACCESS EXCLUSIVE for the length of the rewrite. Noted rather
-- than avoided -- the alternative is repeating the expression character for
-- character in every statement that compares a digest, which is two places for
-- one rule to live and is what this column exists to prevent.
ALTER TABLE issues ADD COLUMN embedding_source_digest TEXT
    GENERATED ALWAYS AS (
        md5(coalesce(title, '')) || md5(coalesce(description, ''))
    ) STORED;


-- One embedding per issue.
--
-- A side table rather than a `vector` column on `issues`, and the reason is
-- the write pattern rather than tidiness. `issues` is the hottest table in the
-- schema -- every board read, every filter, every keyset page walks it -- and
-- an embedding is 384 floats, 1544 bytes, which is far past the ~2KB TOAST
-- threshold for a row that is otherwise a few hundred bytes. Putting it inline
-- would push a large share of `issues` rows out to TOAST and make every read
-- that never wanted the vector pay for the wider heap. The embedding is also
-- rewritten on a completely different schedule from the issue -- by a refresh
-- sweep, long after the edit -- so an inline column would rewrite an `issues`
-- row (and every index entry over it) for a change to data no product read
-- selects.
--
-- One row per issue, not one per (issue, model). Two models' vectors are two
-- coordinate systems and are never compared with each other, so a table
-- holding both would need every read to filter by model anyway -- and the only
-- thing the second row could be used for is the model nothing is querying
-- with. Re-embedding under a new model overwrites, and the `model` column
-- below is what makes the overwrite detectable rather than silent.
CREATE TABLE issue_embeddings (
    -- No surrogate id: the row IS its key. An `id UUID PRIMARY KEY` would need
    -- a UNIQUE (workspace_id, issue_id) beside it to stop one issue having two
    -- embeddings -- a second key that buys nothing and one more column for a
    -- writer to get wrong. The same call `initiative_projects` makes in 022.
    --
    -- `workspace_id` leads for the reason every table here has it lead: the
    -- primary key is also the index that answers "this workspace's
    -- embeddings", which is the ONLY read this table has. See the index
    -- discussion at the foot of this file for why that matters more here than
    -- it does anywhere else in the schema.
    workspace_id UUID NOT NULL,
    issue_id UUID NOT NULL,

    -- Which embedder produced the vector, as it names itself -- e.g.
    -- 'hashing-v1' or 'sentence-transformers/all-MiniLM-L6-v2'.
    --
    -- NOT NULL and never defaulted, because a vector whose model is unknown is
    -- a vector that cannot be compared with anything. Two models embed into
    -- two different 384-dimensional spaces; a cosine distance between them is
    -- a number with no meaning, and -- worse -- a plausible-looking number
    -- rather than an error. Every read in app/repositories/embeddings.py
    -- therefore carries `model = $n` for the model the CALLER is querying
    -- with, so switching embedders invalidates every stored vector at once
    -- without a migration and without a backfill: the old rows simply stop
    -- matching, which is the same mechanism the digest above uses.
    model TEXT NOT NULL,

    -- The value `issues.embedding_source_digest` had when this vector was
    -- built. Freshness is `source_digest = issues.embedding_source_digest`,
    -- evaluated in the join; see the long note on that column.
    --
    -- Stored here rather than compared against a copy of the text, because the
    -- text is what this table exists NOT to duplicate: a copy of every title
    -- and description would be a second place the issue's content lives, which
    -- is precisely the reconciliation problem 011 declined a search engine to
    -- avoid.
    source_digest TEXT NOT NULL,

    -- 384 dimensions, matching app.domain.semantic_search.EMBEDDING_DIMENSIONS.
    --
    -- Not a round number picked here: it is the output width of the small
    -- English sentence encoders worth running locally on a CPU --
    -- all-MiniLM-L6-v2 and bge-small-en-v1.5 are both 384 -- so the schema
    -- accepts a real model without a migration on the day one is installed.
    -- The deterministic fallback in app/services/embeddings.py hashes into the
    -- same width for exactly that reason.
    --
    -- The dimension is IN THE TYPE rather than left as an unconstrained
    -- `vector`. A bare `vector` column accepts any width and defers the
    -- mismatch to the first comparison, where it surfaces as a runtime error
    -- from a distance operator on a query that used to work; `vector(384)`
    -- refuses the insert, which is the moment the mistake is cheap.
    --
    -- NOT NULL. "No embedding yet" is the absence of the ROW, which the
    -- LEFT JOIN in the stale sweep already reads; a NULL vector would be a
    -- second spelling of the same state and the two would need reconciling.
    embedding vector(384) NOT NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT issue_embeddings_pkey PRIMARY KEY (workspace_id, issue_id),

    -- Composite, through `workspace_id`, onto `issues_workspace_id_key` from
    -- 007 -- the pattern 002 establishes and every table since restates. A
    -- single-column `REFERENCES issues (id)` would check that the issue is
    -- real somewhere and say nothing about where, so a row could pair
    -- workspace A's tenancy column with workspace B's issue and every
    -- constraint would report success. There is one workspace_id column on
    -- this row and both the primary key and this foreign key read it, so that
    -- row does not exist.
    --
    -- ON DELETE RESTRICT rather than CASCADE, following 009, 010 and 022.
    -- Nothing in this application deletes an issue today -- issues are
    -- archived -- so the RESTRICT is a guard on a path that does not exist
    -- yet, and it is the correct guard: the day one is written, it has to
    -- clear the embedding in the same transaction rather than have it vanish
    -- as an invisible side effect.
    CONSTRAINT issue_embeddings_issue_fk
        FOREIGN KEY (workspace_id, issue_id)
        REFERENCES issues (workspace_id, id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    -- A bound, not a validation. The model name comes from the deployment's
    -- own configuration rather than from a client, so its content is not a
    -- trust question; an unbounded TEXT column is still a way to make a row
    -- arbitrarily wide.
    CONSTRAINT issue_embeddings_model_length
        CHECK (length(model) BETWEEN 1 AND 200),

    -- Exactly what `issues.embedding_source_digest` produces: two md5s, so 64
    -- lowercase hex characters. Written as a format check rather than a length
    -- one because the value's whole job is to be compared for equality with
    -- that column, and a digest of some other shape can only ever compare
    -- unequal -- which would present as an embedding that is permanently stale
    -- and permanently re-queued, with nothing reporting an error.
    CONSTRAINT issue_embeddings_source_digest_format
        CHECK (source_digest ~ '^[0-9a-f]{64}$')
);


-- ------------------------------------------------------------------
-- THE INDEX DECISION: why there is no HNSW or IVFFlat index here
-- ------------------------------------------------------------------
--
-- pgvector offers two approximate-nearest-neighbour index types, and this file
-- deliberately builds neither. That is the decision most likely to be read as
-- an omission, so here is the argument in full.
--
-- What both of them index is the WHOLE table. An ANN index is a data structure
-- over every vector in `issue_embeddings` -- HNSW a navigable small-world
-- graph, IVFFlat a set of lists around centroids computed from the whole
-- column. Neither can carry `workspace_id` as a scan key: pgvector has no
-- composite operator class, so there is no `USING hnsw (workspace_id,
-- embedding vector_cosine_ops)` to write, the way 011 could write
-- `USING GIN (workspace_id, search_vector)` because btree_gin gave GIN a UUID
-- equality operator class.
--
-- So a tenant-scoped ANN query is: walk a graph built over every tenant's
-- vectors, take the globally nearest candidates, and THEN apply
-- `workspace_id = $1`. The tenant predicate is a post-filter over a top-k, and
-- that has two consequences of very different severity:
--
--   * It is NOT a leak. The predicate is still in the WHERE clause, so no
--     other tenant's row survives it -- not into the result, not into the
--     count, not into the ordering. A plan that reads another workspace's
--     vector and discards it has disclosed nothing, because nothing about it
--     reaches the caller.
--
--   * It IS a correctness defect, and precisely for the smallest tenants. If
--     `ef_search` candidates are drawn from the whole installation and a
--     workspace holds one row in ten thousand, `LIMIT 20` comes back with two
--     results, or none -- while twenty perfectly good matches sit in the
--     table. A new workspace's duplicate detection would silently do nothing,
--     get better as the workspace grew, and be impossible to distinguish from
--     "there are no similar issues".
--
-- pgvector 0.8 added iterative index scans (`hnsw.iterative_scan`) for exactly
-- this, and they do help: the scan resumes until enough rows pass the filter.
-- They do not close it -- `hnsw.max_scan_tuples` bounds the resumption, so a
-- sufficiently small tenant in a sufficiently large installation still
-- under-fills -- and they are a session GUC, which on a pooled connection has
-- to be `SET LOCAL` inside a transaction that a single-SELECT read does not
-- otherwise need.
--
-- The alternative is an exact scan of one workspace's rows, which is what this
-- file chooses. `issue_embeddings_pkey` restricts to the tenant on its leading
-- column and the distance is computed over what is left. It is O(rows in this
-- workspace) rather than O(log rows in the installation), and it has the two
-- properties the approximate version does not: exact recall, and a cost that
-- does not depend on how many other tenants exist. 384 floats is one 1.5KB
-- comparison; ten thousand of them is a few milliseconds and reads about 15MB,
-- which is a large workspace's entire embedding table and is the kind of thing
-- shared_buffers is for.
--
-- Between HNSW and IVFFlat, for the record and for whoever adds one: HNSW.
-- IVFFlat's lists are k-means centroids over the whole column, so a
-- workspace's rows scatter across lists chosen by the shape of every OTHER
-- tenant's data, and recall then depends on `probes` in a way nobody can tune
-- per tenant. It also has to be built AFTER the table is populated -- built
-- here, in a migration, it would train on zero rows -- and re-trained as the
-- distribution shifts. HNSW builds incrementally, needs no training set, and
-- holds recall as rows arrive.
--
-- The upgrade path, so that "exact scan" is a decision with an exit and not a
-- ceiling: PARTITION `issue_embeddings` BY HASH (workspace_id) or by LIST for
-- the large tenants, and build a per-partition HNSW index with
-- `vector_cosine_ops`. Then the tenant is the partition rather than a filter,
-- partition pruning happens before the index is consulted, and the graph being
-- walked contains one workspace's vectors and no others -- which removes both
-- the under-fill and the "another tenant's growth slows my search" property in
-- one move. Do it when a single workspace's embedding count makes the exact
-- scan measurable, not before: partitioning is a rewrite of this table and of
-- every foreign key into it.
--
-- The distance operator is `<=>` -- cosine -- and the operator class an index
-- would use is `vector_cosine_ops`. Both candidate embedders return unit
-- vectors, on which cosine and L2 (`<->`) produce the same ORDER, so this
-- looks like a free choice and is not: nothing in this schema forces
-- normalisation, and `<=>` is invariant to magnitude while `<->` is not. A
-- backend that one day returns unnormalised vectors keeps ranking by direction
-- under `<=>` and starts ranking partly by length under `<->`. `<#>` (negative
-- inner product) is worse still for the same reason -- it rewards long vectors
-- outright. Similarity is reported to clients as `1 - (a <=> b)`, which for
-- unit vectors is the cosine in [-1, 1] and in practice in [0, 1]; see
-- app/domain/semantic_search.py for why that number is called a similarity and
-- never a probability.
--
-- ------------------------------------------------------------------
--
-- The one index this file adds, then. It answers the regeneration sweep --
-- "which of this workspace's issues have no current embedding under model
-- $n" -- which is a LEFT JOIN whose join condition is
-- (workspace_id, issue_id, model, source_digest). The primary key covers the
-- first two; adding the other two makes the anti-join index-only, so the sweep
-- reads index pages instead of fetching a 1.5KB heap row per issue purely to
-- discover that its digest still matches.
--
-- `model` before `source_digest` because model equality is the selective half
-- -- one deployment runs one model, so every row matches -- and because a
-- digest is a 64-byte value that would otherwise sit between the tenant and
-- the column the join actually discriminates on.
CREATE INDEX issue_embeddings_workspace_model_digest_idx
    ON issue_embeddings (workspace_id, model, source_digest);


-- The referencing side of issue_embeddings_issue_fk is
-- issue_embeddings_pkey's own (workspace_id, issue_id), so unlike 022 and 009
-- this file adds no separate index for it: the primary key IS that index, and
-- a second one over the same two columns in the same order would be a copy the
-- planner never chooses and every INSERT still maintains.
--
-- There is deliberately no index on `issues.embedding_source_digest`. Nothing
-- looks an issue up BY its digest; the digest is read from a row that some
-- other predicate already selected -- `workspace_id = $1` -- and compared. An
-- index over it would be maintained by every title edit in the installation
-- and read by nothing.
