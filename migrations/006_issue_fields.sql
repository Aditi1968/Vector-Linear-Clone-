-- Issue fields: who it is for, who filed it, how big it is, when it is due,
-- and whether it is still on the board.
--
-- Five nullable columns and two foreign keys. Nothing here is backfilled and
-- nothing becomes NOT NULL, so unlike 002 and 005 this file imposes no
-- constraint that an existing row has to be prepared for first. That is not
-- laziness about data quality -- it is what these columns mean. An issue with
-- no assignee is the ordinary state of an issue, not a half-migrated one, and
-- an issue that predates `users` has no creator that any backfill could
-- honestly invent.
--
-- The one idea worth reading this file for is issues_assignee_fk. "An assignee
-- must belong to the issue's workspace" is a tenant-isolation rule, and this
-- migration makes the server the thing that enforces it, in the same statement
-- as the write, rather than a SELECT the application is trusted to run first.
-- 004 shaped workspace_members's primary key for exactly this.
--
-- Depends on 003 (users), 004 (workspace_members) and 005 (issues.number,
-- issues.workflow_state_id). It references all three and will not apply
-- without them.
--
-- Apply this ONLY through `python -m scripts.apply_migration
-- migrations/006_issue_fields.sql`, for the reasons 002 sets out at length:
-- a hand-run gets no ledger row, no advisory lock, no recorded checksum, and
-- runs statement-at-a-time in autocommit instead of inside the single
-- transaction the runner wraps the file in.


-- --------------------------------------------------------------------------
-- The columns
-- --------------------------------------------------------------------------

-- All nullable, and each NULL means something specific rather than "not
-- filled in yet":
--
--   * assignee_id  -- nobody has picked this up. The normal state of a new
--                     issue, and the state a triage view exists to find.
--   * creator_id   -- no known creator. Every issue that predates 003 is in
--                     this state permanently, and so is every issue whose
--                     creator's account is later deleted; see
--                     issues_creator_fk.
--   * estimate     -- not sized. Distinct from an estimate of 0, which is a
--                     team saying the work is free.
--   * due_date     -- no date has been committed to. Distinct from a date in
--                     the past, which is a commitment that was missed.
--   * archived_at  -- on the board. See the archive section below.
--
-- Separate statements rather than one ALTER TABLE with five actions. The
-- runner holds one transaction over the whole file either way, so this buys
-- no atomicity -- what it buys is that a failure names one column instead of
-- one statement, and that each column can carry the comment that explains it.
ALTER TABLE issues ADD COLUMN assignee_id UUID;

ALTER TABLE issues ADD COLUMN creator_id UUID;

-- INTEGER rather than SMALLINT or NUMERIC. An estimate is a whole number of
-- whatever unit the team has agreed on -- points, hours, t-shirt sizes mapped
-- to integers -- so a fractional type would offer a precision the product has
-- no meaning for. SMALLINT would fit every realistic value and would put a
-- 32767 ceiling in the schema for no reason anyone could point at.
ALTER TABLE issues ADD COLUMN estimate INTEGER;

-- DATE, emphatically not TIMESTAMPTZ. A due date is a calendar day someone
-- committed to, and it is the same day for everyone looking at the issue. As
-- a timestamp it would need a time (midnight in which zone?) and would then
-- shift across the date line: an issue due 'Friday' set by someone in Berlin
-- would read as due Thursday to a colleague in Los Angeles, which is a
-- different promise from the one that was made. The moment something happened
-- is a TIMESTAMPTZ; the day something is expected is a DATE.
ALTER TABLE issues ADD COLUMN due_date DATE;

-- Archival, and the reason this product archives rather than deletes.
--
-- 005 made `number` gapless and UNIQUE per team, and the identifier built from
-- it -- ENG-42 -- is the name the issue is known by everywhere outside this
-- database: in URLs, in commit messages, in code review, in conversation. A
-- row discarded outright takes that name out of circulation permanently. The
-- number is never reissued (the counter only moves forward), so ENG-42 does
-- not come back as something else; it becomes a reference that resolves to
-- nothing, in every place anyone already wrote it down.
--
-- The second reason is structural and arrives with the next few migrations.
-- Comments, attachments, issue relations and history all reference issues, and
-- under hard deletion each one needs its own answer to "what happens to this
-- when the issue goes" -- so deletion becomes a decision repeated per table,
-- where any single CASCADE quietly destroys more than the operator asked for.
-- Archival needs no such answer: the row stays, so every reference to it stays
-- valid.
--
-- The timestamp rather than a boolean, because "when" is the question actually
-- asked of an archived issue -- an archive view is ordered by it, and a
-- restore wants to know how long ago. A boolean would need a second column to
-- answer that, and the two could then disagree.
--
-- This is not a tombstone for permanent deletion. Should a hard delete ever be
-- needed -- a legal erasure request, say -- it is a deliberate operation with
-- its own migration and its own decisions about the referencing tables above,
-- not something an archive flag quietly becomes.
ALTER TABLE issues ADD COLUMN archived_at TIMESTAMPTZ;


-- A negative estimate has no meaning to give it. Zero does -- "we looked at
-- this and it is free" -- so the bound is 0 and not 1.
--
-- There is deliberately no upper bound. Any ceiling here would be a guess at
-- what unit a team estimates in: 21 is absurd in hours and ordinary in points,
-- and 100 is ordinary in either. A limit the product wants is a product
-- policy, enforced where the product knows the team's unit; a limit in the
-- schema is a claim that some whole number of an unknown unit is impossible,
-- which is not a claim this file can make.
ALTER TABLE issues ADD CONSTRAINT issues_estimate_non_negative
    CHECK (estimate >= 0);


-- --------------------------------------------------------------------------
-- The assignee, and why this is a foreign key rather than a check in a service
-- --------------------------------------------------------------------------

-- An issue may only be assigned to a member of its own workspace, and this
-- constraint is the whole enforcement of that. There is no SELECT anywhere in
-- the application that asks "is this user a member of this workspace" before
-- an assignment, on purpose.
--
-- Such a pre-check would be a second copy of this rule, and a weaker one in
-- two distinct ways. It is a separate statement, so the membership it verifies
-- can be revoked between the check and the write -- the classic
-- time-of-check-to-time-of-use gap, and the window is a network round trip
-- wide. And two copies of a rule are two places that have to agree forever:
-- the day one of them is updated and the other is not, the schema and the
-- service disagree about who may be assigned work, and nothing reports it.
-- Here the check and the write are one statement, evaluated by the server
-- while it holds the row, and there is exactly one copy of the rule.
--
-- The composite key is what makes this a tenant rule rather than merely a
-- referential one. Two single-column keys -- assignee_id to users, workspace_id
-- to workspaces -- would each be satisfied by an issue in workspace A assigned
-- to a real user who is a member only of workspace B. That is precisely the
-- cross-tenant assignment this constraint exists to refuse, and it is refused
-- because the pair is checked as one tuple. 004 declares
-- workspace_members's primary key on (workspace_id, user_id) so that this
-- reference is possible at all: a foreign key may only target a
-- UNIQUE-constrained column set.
--
-- MATCH SIMPLE -- the default -- is load-bearing here, and it is the one place
-- in this schema where its NULL behaviour is wanted rather than tolerated. A
-- composite FK under MATCH SIMPLE skips the check entirely for a row where any
-- referencing column is NULL, so an unassigned issue (assignee_id IS NULL,
-- workspace_id NOT NULL) is exempt and needs no membership to point at. That
-- is exactly right: an issue with no assignee has no assignee to validate.
-- Contrast 002 and 005, where the same behaviour is a hazard and the columns
-- are NOT NULL specifically to keep it from switching the check off.
--
-- ON DELETE SET NULL (assignee_id), with the column list, and the list is not
-- optional. A bare ON DELETE SET NULL nulls *every* referencing column, which
-- here includes issues.workspace_id -- NOT NULL since 002 -- so a membership
-- deletion would fail on the constraint rather than doing what it says.
-- Naming the column confines the effect to the assignment. (PostgreSQL 15
-- added the column list; this project is pinned to 18.)
--
-- SET NULL rather than RESTRICT, because removing someone from a workspace is
-- an ordinary operation and it must not be blocked by their open work.
-- Unassigning that work is the correct outcome -- the issue survives, and lands
-- in exactly the state a never-assigned issue is in, which the product already
-- renders and which a triage view already surfaces. RESTRICT would mean an
-- offboarding cannot complete until someone hunts down every issue the leaver
-- held, and the pressure that creates is to reassign in haste or to delete the
-- issues.
--
-- The reach of that SET NULL is bounded, and 004 is what bounds it:
-- workspace_members references both workspaces and users with ON DELETE
-- RESTRICT, so nothing cascades *into* workspace_members. This clause can only
-- fire from a statement that deletes a membership outright -- that is, from an
-- offboarding -- and never as a third-order effect of deleting a workspace or
-- a user.
--
-- ON UPDATE RESTRICT, matching every other key in this schema: relocating a
-- membership's key columns is not something to have silently rewrite issues.
ALTER TABLE issues ADD CONSTRAINT issues_assignee_fk
    FOREIGN KEY (workspace_id, assignee_id)
    REFERENCES workspace_members (workspace_id, user_id)
    ON DELETE SET NULL (assignee_id) ON UPDATE RESTRICT;


-- The creator references users directly, and NOT workspace_members. The
-- difference from the assignee above is not an oversight; the two columns
-- record different kinds of fact.
--
-- "Who is assigned" is a live claim about the present, and it stops being true
-- the moment that person leaves the workspace -- so tying it to membership is
-- what keeps it honest. "Who filed this" is a historical fact about a moment
-- that has already happened. It stays true after the author leaves, and a
-- constraint that required the creator to be a current member would either
-- forbid offboarding anyone who ever filed an issue, or -- under SET NULL --
-- erase the authorship of every issue they wrote on the way out. Neither is a
-- thing to do to a record of what happened.
--
-- The creator being a member of the workspace at the time of writing is
-- enforced where that fact is live: the create path can only obtain a
-- creator from an authenticated caller who has already been authorised for
-- the workspace. Recording it here does not re-assert it forever.
--
-- ON DELETE SET NULL, and here the bare form is right because there is one
-- referencing column. Deleting a user account moves their issues to
-- creator_id IS NULL, which is a state this column already defines and which
-- every issue predating 003 is already in -- so the read path needs no new
-- case. RESTRICT would be worse than strict: with no statement anywhere able
-- to clear the column, an account that ever filed an issue could not be
-- deleted at all without an application-level scrub written for the purpose,
-- and account deletion is not an operation to make conditional on a
-- product-level cleanup.
ALTER TABLE issues ADD CONSTRAINT issues_creator_fk
    FOREIGN KEY (creator_id)
    REFERENCES users (id)
    ON DELETE SET NULL ON UPDATE RESTRICT;


-- --------------------------------------------------------------------------
-- Indexes
-- --------------------------------------------------------------------------

-- PostgreSQL indexes the referenced side of a foreign key and never the
-- referencing side, so without this every deletion from workspace_members
-- scans issues in full to find the rows its SET NULL has to rewrite. The
-- column order is issues_assignee_fk's column list in the FK's order, because
-- that is the lookup the referential action performs.
--
-- The same index is what "issues assigned to this person in this workspace"
-- reads -- the query behind every My Issues view -- so the FK's requirement
-- and the product's hottest filtered listing want the identical index. It is
-- deliberately not widened to (workspace_id, assignee_id, created_at DESC,
-- id DESC): a keyset walk within one assignee is a query nothing issues yet,
-- and two more key columns is write amplification on every insert and every
-- reassignment, paid now against a page that does not exist.
CREATE INDEX issues_workspace_assignee_idx
    ON issues (workspace_id, assignee_id);

-- The referencing side of issues_creator_fk, for the same reason. Keyed on
-- creator_id alone and NOT on (workspace_id, creator_id), which is the column
-- order the other index above uses: the referential action here fires from
-- `users` and knows only the user id, so a leading workspace_id would leave it
-- unable to use this index at all and back to scanning issues.
CREATE INDEX issues_creator_idx ON issues (creator_id);

-- The default issue list, which is the hottest query the product runs, is
-- 002's keyset walk with `archived_at IS NULL` added. This is 002's index with
-- that predicate baked in.
--
-- The partial form is the point. Under the unpartitioned index the walk has to
-- read archived entries and discard them, so the cost of returning fifty live
-- issues grows with the number of issues the workspace has EVER created rather
-- than with the number it currently has on the board. That is the same defect
-- 002 describes when it drops 001's index -- a cost proportional to rows the
-- query is being changed to stop wanting -- and archived issues are not a rare
-- case to design around: in a tracker, archived is where issues end up, so in
-- the steady state most rows in this table are ones this query must skip.
-- Entries leave this index when an issue is archived, so it stays proportional
-- to the live board.
--
-- 002's issues_workspace_created_at_id_idx is deliberately left in place, and
-- not because dropping it is out of scope. It is the index behind the archive
-- view -- `archived_at IS NOT NULL`, ordered the same way -- which is the read
-- that makes restoring possible and which this partial index cannot serve at
-- all. The alternative is a second partial index on the complement, which is
-- three indexes over the same columns where two suffice.
CREATE INDEX issues_workspace_live_created_at_id_idx
    ON issues (workspace_id, created_at DESC, id DESC)
    WHERE archived_at IS NULL;
