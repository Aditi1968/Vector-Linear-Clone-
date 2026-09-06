-- Search: one query box over the two things a workspace is made of, answered
-- by the database that already holds them.
--
-- PostgreSQL's own full-text search, and deliberately no Typesense, no
-- Elasticsearch and no managed search API. Every one of those is a second
-- store holding a copy of this data, which means an indexing pipeline, a
-- backfill, a reconciliation job for when the two disagree, and a tenant
-- boundary re-implemented in a system whose query language has never heard of
-- `workspace_id`. A generated column is none of that: the vector is derived
-- from the row by the same transaction that writes the row, so a title and
-- its index cannot be out of step, and a search is an ordinary tenant-scoped
-- SELECT with the same WHERE clause every other read here carries.
--
-- What that choice costs, stated rather than discovered later: no typo
-- tolerance, no synonyms beyond the `english` dictionary's stemming, and
-- ranking by term weight rather than by anything learned from what people
-- click. Those are the reasons to reach for a search engine, and none of them
-- is a reason to have two copies of the data today.
--
-- Apply this ONLY through `python -m scripts.apply_migration
-- migrations/011_search.sql`. The runner wraps the file in one transaction,
-- takes an advisory lock and records a checksum; a hand-run gets none of that
-- and executes statement-at-a-time in autocommit, which for this file means a
-- table rewritten with a generated column and no index over it -- every search
-- a sequential scan, with nothing recording that the file only half-ran.
--
-- DEPENDS ON 002, which puts `workspace_id` on `issues`; on 006, which adds
-- `issues.archived_at` and establishes the live-issue partial index this
-- file's issue index copies; and on 009, which creates `projects`. Applying
-- this file before any of them fails on a column that does not exist and --
-- inside the single transaction the runner wraps the file in -- leaves nothing
-- behind. That failure IS the dependency check: the ledger records what has
-- been applied and not what depends on what, so the schema itself is what has
-- to refuse an out-of-order apply.


-- Required by both indexes below, and by nothing else in this file.
--
-- One sentence of justification, since an extension nothing uses is a
-- dependency for nothing: stock GIN has no operator class for UUID equality,
-- so without btree_gin `workspace_id` cannot be part of a GIN key at all --
-- and every search this schema serves is tenant-scoped, so the tenant
-- equality belongs inside the access method rather than above it.
--
-- Guarded, for the reasons migrations/008_cycles.sql gives at length about
-- btree_gist. Rule 2 of tests/test_migration_lint.py bans defensive DDL
-- because a guard reports success without establishing that the existing
-- object matches the one the migration describes, and an extension has no
-- shape for the guard to hide -- it is present or absent, and neither spelling
-- pins a version. The exemption is anchored to CREATE EXTENSION alone.
--
-- Bare would also be actively wrong against a database where the platform
-- pre-installed the extension: `CREATE EXTENSION btree_gin` errors on
-- "already exists" before reaching the privilege check, and the ledger never
-- records a migration that errored, so the deployment is stuck. btree_gin is
-- trusted on PostgreSQL 13+ and transactional, so it can be created and used
-- in this same file.
CREATE EXTENSION IF NOT EXISTS btree_gin;


-- The searchable rendering of an issue, maintained by the server.
--
-- GENERATED ALWAYS ... STORED rather than a trigger. A trigger would be a
-- second place the rule lives, it would need a PL/pgSQL body -- which rule 1
-- of the linter cannot read past, because a function body's `BEGIN` is
-- indistinguishable from transaction control to a regex -- and it can be
-- disabled. A generated column cannot be written by any statement at all, so
-- there is no INSERT or UPDATE anywhere in this system, now or later, that can
-- leave an issue out of the index or put stale text into it.
--
-- Two weights, which is the entire ranking model: a term in the title (A)
-- outranks the same term in the description (B). That is the difference
-- between "the issue about deploys" and "an issue that mentions deploys in
-- paragraph four", and it is worth having before anything cleverer.
--
-- `to_tsvector('english', ...)` with the configuration named, not the
-- one-argument form. The one-argument form reads `default_text_search_config`,
-- which makes it STABLE rather than IMMUTABLE -- PostgreSQL refuses it in a
-- generated column outright, and the refusal is the useful kind: a session
-- setting deciding how a row is indexed would mean two rows written by two
-- connections indexed under two dictionaries. The query side has to name the
-- same configuration; see app/repositories/issues.py.
--
-- `coalesce` because `description` is nullable, and NULL anywhere in a
-- generated expression makes the whole column NULL -- which is not an error,
-- it is an issue that silently matches nothing.
--
-- No NOT NULL, and none is needed: `coalesce` already makes the expression
-- total, so the constraint would restate the expression rather than constrain
-- it. Rule 4 of the linter refuses it anyway, and correctly -- a column added
-- NOT NULL with no DEFAULT is unsatisfiable for the rows already there,
-- whether or not this particular expression happens to fill them.
--
-- This ALTER rewrites the table. A stored generated column has to be computed
-- for every existing row, so unlike 009's nullable ADD COLUMN this is not a
-- catalog-only change: it takes ACCESS EXCLUSIVE for the length of the
-- rewrite. Noted rather than avoided -- the alternative, an expression index
-- over the same `setweight(...) || setweight(...)` text, moves the same work
-- into an index build and then requires every query to repeat the expression
-- character for character or silently lose the index.
ALTER TABLE issues ADD COLUMN search_vector TSVECTOR
    GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(title, '')), 'A')
        || setweight(to_tsvector('english', coalesce(description, '')), 'B')
    ) STORED;

-- The same shape for projects: the name is what a project is known by, the
-- description is context.
ALTER TABLE projects ADD COLUMN search_vector TSVECTOR
    GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(name, '')), 'A')
        || setweight(to_tsvector('english', coalesce(description, '')), 'B')
    ) STORED;


-- Composite AND partial, because the search query is both.
--
-- The alternative is a bare `GIN (search_vector)`, which PostgreSQL can still
-- use for a tenant-scoped search: it bitmap-ANDs the term list with one of the
-- `(workspace_id, ...)` btrees 002, 006 and 009 already build, so the tenant
-- equality still ends up in an index condition rather than a filter. Measured
-- on postgres:18 against 20,000 issues split evenly between two workspaces,
-- searching a term on one row in a hundred:
--
--     bare GIN + BitmapAnd:      cost 399, 215 buffers, two index scans
--     GIN (workspace_id, ...):   cost 292, 209 buffers, one index scan
--
-- The composite wins because the tenant is a scan key rather than a second
-- bitmap to build and intersect, and the gap widens with the neighbour: the
-- bare index reads the postings for a term across every workspace in the
-- installation and discards the other tenants' afterwards, so a small
-- workspace's searches get slower as a large one grows. That is the one
-- property a multi-tenant index must not have, and it is the reason to spend
-- an extension on this.
--
-- A warning for whoever re-runs that measurement, because it is what made the
-- first attempt say the opposite. A GIN index accumulates new entries in a
-- pending list rather than in the tree, and while that list is unflushed every
-- GIN scan is costed as "plus a scan of the whole pending list" -- so a
-- freshly bulk-loaded index looks expensive and the planner walks past it.
-- VACUUM the table before believing any EXPLAIN of a GIN index; in a running
-- system autovacuum is what does this.
--
-- Column order inside a GIN key is not the btree question it looks like. GIN
-- has no leading column and no ordering, so both keys are looked up
-- independently and intersected. `workspace_id` is written first to match
-- every other index in this schema and to read as "tenant, then term"; the
-- planner is indifferent.
--
-- `WHERE archived_at IS NULL` copies 006's
-- issues_workspace_live_created_at_id_idx and copies its argument with it.
-- Archiving removes an issue from the product, so no search may return one,
-- and the predicate is written into the index rather than applied above it --
-- an archived issue is then absent from the postings instead of being fetched
-- and discarded, so a search costs what a workspace still has on its board
-- rather than everything it has ever filed.
--
-- Unlike the partial indexes 009 declined, this predicate is safe to rely on:
-- it is matched against a WHERE clause this repository writes -- every read in
-- IssueRepository carries `archived_at IS NULL` verbatim -- and not against a
-- referential-integrity check that PostgreSQL generates for itself.
CREATE INDEX issues_workspace_live_search_idx
    ON issues USING GIN (workspace_id, search_vector)
    WHERE archived_at IS NULL;

-- The same, without the partial predicate: `projects` has no archived_at, and
-- a project's terminal states ('completed', 'canceled') are still states a
-- workspace searches for. Adding a predicate over `state` would decide, in the
-- schema, which projects are worth finding.
CREATE INDEX projects_workspace_search_idx
    ON projects USING GIN (workspace_id, search_vector);


-- There is deliberately no index here for the identifier path, and no
-- pg_trgm.
--
-- Typing `ENG-42` has to find that issue, and a tsquery is the wrong tool for
-- it: the identifier is not prose, it is a key, and the exact lookup already
-- has indexes. migrations/005_team_workflows.sql makes the argument in full at
-- its own foot -- `ENG-42` resolves to a team by (workspace_id, key), which
-- teams_workspace_key_unique indexes, and then to an issue by
-- (team_id, number), which issues_team_number_key indexes. Two index lookups,
-- both already paid for.
--
-- pg_trgm would buy prefix and fuzzy matching on top of that -- `ENG-4` while
-- the user is still typing, `EMG-42` for a typo -- and neither is implemented,
-- so installing the extension would add a dependency to the deployment in
-- exchange for nothing running. It is one statement to add on the day a
-- resolver actually issues a `%` or `<->` query.
