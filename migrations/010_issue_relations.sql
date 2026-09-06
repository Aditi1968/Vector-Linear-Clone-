-- Sub-issues and issue relations: the two kinds of edge between issues.
--
-- One column and one table, because the two edges are not the same shape and
-- pretending otherwise costs more than it saves. A sub-issue edge is a
-- property OF an issue -- an issue has at most one parent, and that fact
-- belongs on the row -- while a relation is a fact ABOUT a pair and belongs
-- to neither end. Modelling the parent as a row in issue_relations would
-- make "at most one parent" a constraint nothing in this schema can express;
-- modelling relations as columns on issues would cap them at however many
-- columns someone guessed.
--
-- Two product rules are written into constraints here rather than into
-- prose, because prose is not enforced:
--
--   * a sub-issue MAY belong to a different TEAM than its parent, and MUST
--     belong to the same WORKSPACE. Both halves come out of one foreign key
--     and specifically out of its column list -- see issues_parent_fk.
--   * a relation joins two issues in one workspace and nothing else.
--
-- What this file does NOT enforce, stated here so nobody reads the
-- constraints below as covering more than they do: cycles among sub-issues
-- of depth greater than one. issues_parent_not_self refuses A -> A. Nothing
-- declarative can refuse A -> B -> A, because no CHECK constraint may read
-- another row. That guard lives in RelationService.set_parent, which walks
-- the ancestor chain under a per-workspace advisory lock; see the block
-- comment above issues_parent_fk for exactly what that does and does not
-- buy.
--
-- Apply this ONLY through `python -m scripts.apply_migration
-- migrations/010_issue_relations.sql`, for the reasons 002 spells out at
-- length: the ledger row, the advisory lock, the recorded checksum, and --
-- the expensive one -- the single transaction the runner wraps the file in.
-- Every statement below is DDL over a populated `issues` table, and a
-- statement-at-a-time run that failed partway would leave the column added,
-- the constraints absent, and no record that anything happened.


-- [FK TARGET -- DECLARED IN 007, NOT HERE] The three foreign keys below all
-- reference `issues (workspace_id, id)`, and PostgreSQL allows a foreign key
-- to reference only a UNIQUE-constrained column SET. That constraint is
-- `issues_workspace_id_key`, and this file DEPENDS on it rather than
-- declaring it: migrations/007 adds it, for exactly the reason 002 adds
-- teams_workspace_id_key.
--
-- It looks redundant beside `issues_pkey` on (id) and is not. A unique key
-- on (id) alone says nothing about which workspace that id sits in, so a
-- single-column FK to it would let a relation join two issues in two
-- different tenants while every constraint in the database still reported
-- success. The whole tenant guarantee of this file rests on that one
-- constraint existing.
--
-- MERGE NOTE (2026-09-06): 007 declares it because 007 is the first
-- migration that needs it -- its `issue_labels` and `comments` tables hang
-- composite foreign keys off `issues` the same way this file does. 006 does
-- not, and could not have been the natural home for it: its own composite
-- keys reference `workspace_members` rather than `issues`.
--
-- Exactly one migration may declare it. Two declarations means the second to
-- run fails on the duplicate name, and two identical unique indexes on one
-- table would be write amplification with no reader. So anything later than
-- 007 that needs an issue's tenant pair -- this file, and project membership
-- elsewhere -- depends on it and must not re-add it.
--
-- 010 REQUIRES 007. Applying the chain with 007 skipped fails at the first
-- composite foreign key below, with "there is no unique constraint matching
-- given keys for referenced table". That error names this dependency; it is
-- not a defect in either file. 007 sorts ahead of 010, so a chain applied in
-- filename order already satisfies it.


-- Nullable, and permanently so: "has no parent" is the ordinary state of an
-- issue, not a gap waiting to be backfilled. No DEFAULT, because there is no
-- sensible issue to default a parent to and a default here would silently
-- attach every future insert to it.
--
-- Nullability is also what makes the composite foreign key below behave
-- correctly rather than dangerously. 002 warns that a composite FK is MATCH
-- SIMPLE and skips its check entirely when a referencing column is NULL, and
-- there that was a hazard -- it is why issues.workspace_id and
-- issues.team_id are NOT NULL. Here it is the mechanism: workspace_id is
-- already NOT NULL, so the only column that can be NULL in the pair is
-- parent_id, and MATCH SIMPLE skipping the check for exactly those rows is
-- precisely "an issue with no parent references nothing".
ALTER TABLE issues ADD COLUMN parent_id UUID;


-- Self-parenting, refused by the server.
--
-- IS DISTINCT FROM rather than `<>`: a plain inequality against a NULL
-- parent_id evaluates to NULL, a CHECK constraint treats NULL as satisfied,
-- and the two therefore happen to agree here -- but only by accident, and
-- the accident stops holding the moment this expression grows a second
-- conjunct. IS DISTINCT FROM says what is meant: a parent that is not this
-- row, or no parent at all.
ALTER TABLE issues ADD CONSTRAINT issues_parent_not_self
    CHECK (parent_id IS DISTINCT FROM id);


-- The parent must be an issue in the SAME WORKSPACE, and may be in ANY TEAM.
--
-- Both halves of the product rule are in the column list and nowhere else.
-- The key references issues (workspace_id, id): workspace_id appears, so a
-- parent in another tenant has no matching row and the server refuses the
-- write; team_id does NOT appear, so a parent in another team of the same
-- workspace matches like any other. Adding team_id to this list would be a
-- one-word change that silently outlaws cross-team sub-issues, which is why
-- there is a test named for that case rather than only for the refusal.
--
-- ON DELETE RESTRICT, matching 002 and for the same reason turned around:
-- CASCADE here would make `DELETE FROM issues WHERE id = <parent>` delete
-- the entire sub-tree beneath it, recursively, reporting `DELETE 1`. There
-- is no issue-deletion path in the product today, so RESTRICT costs nothing
-- now and forces whoever adds one to decide deliberately between refusing
-- the delete, re-parenting the children, and orphaning them -- a product
-- question that must not be answered by a default in a migration.
--
-- ON UPDATE RESTRICT for 002's mirror-image reason: nothing may relocate an
-- issue's tenant key out from under the rows pointing at it.
--
-- ------------------------------------------------------------------
-- What this does NOT prevent
-- ------------------------------------------------------------------
--
-- Cycles of length two or more. A -> B -> A satisfies this key at every
-- step: both rows are in the workspace, both name a real issue, neither
-- names itself. A CHECK constraint sees one row, so it cannot see the other
-- end of the chain, and the only declarative mechanisms that could -- a
-- trigger walking the ancestry, or a materialised path column -- are both
-- more machinery than this migration should introduce and neither is free
-- of the same race a trigger has.
--
-- So the multi-level guard is in the service, and it is worth being exact
-- about its strength because "we prevent cycles" is easy to over-claim:
--
--   * RelationService.set_parent takes pg_advisory_xact_lock over the
--     workspace, walks the proposed parent's ancestors with a recursive
--     query, and refuses the write if the child appears among them. That
--     covers cycles at ANY depth, not just two.
--   * It is race-free only because every writer takes the same lock. Two
--     concurrent re-parents in one workspace serialise, so neither can read
--     an ancestry the other is about to change.
--   * It is therefore only as strong as the rule that all parent_id writes
--     go through that method. A future bulk import, an issueCreate that
--     accepts a parent, or a hand-run UPDATE can each write a cycle this
--     database will accept. When one of those is added it must take the same
--     lock, or this guarantee is gone with no failing test to say so.
ALTER TABLE issues ADD CONSTRAINT issues_parent_fk
    FOREIGN KEY (workspace_id, parent_id)
    REFERENCES issues (workspace_id, id)
    ON DELETE RESTRICT ON UPDATE RESTRICT;


-- Two readers, one index.
--
-- The product read is "this issue's sub-issues, newest first", so the
-- ordering columns are carried in the index rather than left to a sort:
-- (workspace_id, parent_id) is the equality prefix and (created_at DESC,
-- id DESC) is the keyset, the same shape as
-- issues_workspace_created_at_id_idx one level down.
--
-- The other reader is PostgreSQL itself. An index is created on the
-- REFERENCED side of a foreign key and not on the REFERENCING side, so
-- without this every delete or key update of an issue would scan the whole
-- table to satisfy issues_parent_fk's RESTRICT check.
--
-- Partial, because most issues have no parent and a NULL parent_id is never
-- searched for: nothing asks for "the children of no issue". The planner can
-- still use a partial index for the FK check and for the product read, since
-- `parent_id = $2` implies `parent_id IS NOT NULL`.
CREATE INDEX issues_workspace_parent_created_at_id_idx
    ON issues (workspace_id, parent_id, created_at DESC, id DESC)
    WHERE parent_id IS NOT NULL;


-- ======================================================================
-- Relations
-- ======================================================================
--
-- ONE CANONICAL ROW PER RELATIONSHIP, not one row per direction. This is the
-- decision the rest of the table is shaped by, so here is the argument.
--
-- The product vocabulary is four names: blocks, blocked_by, related,
-- duplicate. They are not four independent kinds of edge. `blocks` and
-- `blocked_by` are one edge read from its two ends, and `related` and
-- `duplicate` are symmetric -- "A is related to B" and "B is related to A"
-- are the same sentence. Four names, three storable types, and every
-- relationship expressible in exactly two ways by a client.
--
-- Row-per-direction would mean storing both. It reads well -- every query
-- becomes `WHERE source_issue_id = $1` with no UNION -- and it cannot
-- satisfy the requirement that duplicate relations be impossible. A unique
-- constraint on (workspace_id, source, target, type) would accept
-- (A, B, related) and (B, A, related) as different tuples, because they are
-- different tuples; they are simply not different relationships. The
-- constraint would read as enforcing uniqueness while enforcing nothing that
-- matters, which is worse than having no constraint at all. Worse still,
-- every write becomes two rows that some code has to keep in step, and
-- nothing in the database says they must agree: delete one and the
-- relationship exists from one side and not the other.
--
-- So: one row, canonicalised on the way in, and the inverse produced on the
-- way out by reading both columns and flipping the type. The cost is real
-- and is paid in one place -- reads are a UNION ALL of the two directions,
-- and `blocked_by` never appears in this table -- and in exchange
-- issue_relations_unique is a constraint that means what it says.
CREATE TABLE issue_relations (
    id UUID PRIMARY KEY DEFAULT uuidv7(),

    workspace_id UUID NOT NULL,

    source_issue_id UUID NOT NULL,
    target_issue_id UUID NOT NULL,

    -- TEXT with a CHECK rather than a PostgreSQL enum, matching how 001
    -- spells `priority`. An enum's values can only be extended by ALTER TYPE
    -- ADD VALUE, which cannot be used in the same transaction that adds it,
    -- and every migration here runs inside one transaction the runner owns.
    -- A CHECK is edited by dropping and re-adding a constraint, which is
    -- ordinary transactional DDL.
    type TEXT NOT NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- No updated_at, deliberately. A relation has no mutable field: changing
    -- its type or either end makes it a different relationship, so the
    -- operations are create and delete. A column that could never differ
    -- from created_at would be a standing invitation to add an update path
    -- that the unique constraint above has not been designed for.

    -- Three storable types, not four. `blocked_by` is absent because it is
    -- `blocks` read from the other end, and storing both would mean the same
    -- relationship had two legal spellings -- which is the duplicate this
    -- table exists to make impossible. The API still accepts and returns all
    -- four names; the translation happens in the repository.
    --
    -- `duplicate` is treated as symmetric, which is a product reading and
    -- not a fact about the word: it is symmetric here because the vocabulary
    -- names no inverse for it, so there is no way for a client to express a
    -- direction. If `duplicate_of` is ever added, this constraint and
    -- issue_relations_symmetric_ordered both change together -- `duplicate`
    -- moves to the directed side and its rows need canonicalising, which is
    -- a data migration and not a one-line edit.
    CONSTRAINT issue_relations_type_known
        CHECK (type IN ('blocks', 'related', 'duplicate')),

    -- An issue cannot block, duplicate or relate to itself.
    --
    -- Both columns are NOT NULL, so this is a plain inequality with no NULL
    -- case to reason about, unlike issues_parent_not_self above.
    CONSTRAINT issue_relations_not_self
        CHECK (source_issue_id <> target_issue_id),

    -- What makes issue_relations_unique mean something for symmetric types.
    --
    -- For `related` and `duplicate` the two ends are interchangeable, so the
    -- pair is stored in one fixed order -- lower id first -- and the other
    -- order is not representable. (A, B, related) and (B, A, related) are
    -- therefore the same row rather than two rows, and the unique constraint
    -- below refuses the second insert instead of accepting a synonym.
    --
    -- `blocks` is exempt because its two ends are NOT interchangeable:
    -- ordering it would destroy the direction, which is its entire content.
    -- The consequence is that `blocks` may exist in both directions between
    -- one pair -- A blocks B and B blocks A. That is a mutual deadlock and
    -- probably a mistake, but it is a product judgement about workflow, not
    -- an integrity violation, and this file does not refuse it.
    --
    -- So the blocking graph is the one edge in this schema that may contain
    -- a cycle, and nothing prevents it at any layer. Nothing traverses it
    -- today either. Whoever writes the first thing that does -- a dependency
    -- view, a topological sort -- must bring its own cycle guard; see the
    -- note under FOR WHOEVER FIRST TRAVERSES THE BLOCKING GRAPH in
    -- `RelationRepository.list_relations`. Sub-issues are a separate matter
    -- and are guarded on write; the two must not be assumed to share a
    -- guarantee.
    CONSTRAINT issue_relations_symmetric_ordered
        CHECK (type = 'blocks' OR source_issue_id < target_issue_id),

    -- Duplicate relations, refused by the server.
    --
    -- workspace_id leads even though (source, target) already determines the
    -- workspace through the foreign keys below: the constraint's index is
    -- also the access path for `WHERE workspace_id = $1 AND source_issue_id
    -- = $2`, and an index that does not begin with the tenant key cannot
    -- serve a tenant-scoped read.
    --
    -- `type` is part of the key rather than excluded from it, so one pair may
    -- carry several distinct relations at once -- B both blocks A and
    -- duplicates it. Excluding it would silently cap a pair at one
    -- relationship, which is a product rule nobody asked for.
    CONSTRAINT issue_relations_unique
        UNIQUE (workspace_id, source_issue_id, target_issue_id, type),

    -- Both ends must be issues in THIS relation's workspace.
    --
    -- Two composite keys, not two single-column ones. Single-column keys to
    -- issues (id) would each pass while together describing a relation in
    -- workspace A between an issue in A and an issue in B -- a cross-tenant
    -- edge with every constraint reporting success. Because both keys carry
    -- the same workspace_id column of this row, a matching pair is only
    -- possible when both issues sit in that one workspace.
    --
    -- ON DELETE RESTRICT for consistency with every other key in this
    -- schema, and knowing that CASCADE is the more defensible answer HERE
    -- than anywhere else in the database: a relation is meaningless once
    -- either end is gone, and unlike a sub-tree it carries no content of its
    -- own to lose. It is still left as RESTRICT because no issue-deletion
    -- path exists yet, and the agent who writes one should choose CASCADE
    -- explicitly in a migration that says so rather than inherit it from a
    -- guess made here.
    CONSTRAINT issue_relations_source_fk
        FOREIGN KEY (workspace_id, source_issue_id)
        REFERENCES issues (workspace_id, id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    CONSTRAINT issue_relations_target_fk
        FOREIGN KEY (workspace_id, target_issue_id)
        REFERENCES issues (workspace_id, id)
        ON DELETE RESTRICT ON UPDATE RESTRICT
);


-- The two halves of a relation read.
--
-- Reading one issue's relations means finding rows where it is the source
-- OR the target, and an OR across two columns cannot be served by one index.
-- The repository issues that read as a UNION ALL of two equality lookups
-- precisely so each half lands on one of these, and each carries the keyset
-- ordering so neither half needs a sort before the merge.
--
-- The source index would be redundant with issue_relations_unique's leading
-- columns for the FK check alone, and is not redundant for the read: the
-- unique index continues (target_issue_id, type) where this one continues
-- (created_at DESC, id DESC), and only the latter can return a page in
-- cursor order. The target index has no such overlap and is the only thing
-- standing between issue_relations_target_fk and a full scan per delete.
CREATE INDEX issue_relations_workspace_source_created_at_id_idx
    ON issue_relations (workspace_id, source_issue_id, created_at DESC, id DESC);

CREATE INDEX issue_relations_workspace_target_created_at_id_idx
    ON issue_relations (workspace_id, target_issue_id, created_at DESC, id DESC);
