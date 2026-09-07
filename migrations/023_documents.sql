-- Documents, the versions they used to be, and the discussion about them.
--
-- A document is long-form writing that hangs off the workspace, a project or
-- an initiative: a spec, a brief, a decision record. Three tables, and nothing
-- altered on an existing one.
--
-- ------------------------------------------------------------------
-- The shape this file exists to make impossible
-- ------------------------------------------------------------------
--
-- `documents.body TEXT` holding rendered HTML.
--
-- That is the obvious spelling and it is the one that ends with a stored
-- `<script>` in every reader's browser. HTML is a rendering, and a rendering
-- authored by a user is a payload: escaping it on the way out makes the editor
-- useless, and NOT escaping it makes the document a cross-site scripting
-- vector aimed at everybody in the workspace. Sanitising it on the way in is
-- the third option and the worst, because it is a blocklist -- the parser and
-- the sanitiser have to agree about the whole of HTML, forever, and the day
-- they disagree is the day something gets through.
--
-- So `content` is JSONB holding a ProseMirror/TipTap document tree: a closed
-- vocabulary of node types with typed attributes, from which a client BUILDS
-- the rendering. There is no place in that structure for markup to hide,
-- because there is no markup -- a heading is `{"type":"heading",
-- "attrs":{"level":2}}` and cannot be anything else.
--
-- That argument only holds if something checks the structure, which brings us
-- to the honest part of this file.
--
-- ------------------------------------------------------------------
-- What this schema does NOT do about the content, and who does
-- ------------------------------------------------------------------
--
-- The two CHECKs on `content` below say it is a JSON object and that it is not
-- enormous. That is the whole of what the database knows about it. It does not
-- know that `type` is one of eleven strings, that `attrs.level` is an integer
-- between 1 and 6, or -- the one that matters most -- that a link mark's
-- `href` is not `javascript:alert(document.cookie)`.
--
-- Those are enforced in `app.domain.documents.parse_content`, and the reason
-- to state it here rather than leave it implied is that this column is
-- ATTACKER-AUTHORED. Any member who can create a document chooses these bytes.
-- A reader that treated a stored tree as server state -- because it has been
-- sitting in a table long enough to look like one -- would be handing client
-- input straight to another client's renderer.
--
-- So the parse runs in BOTH directions, and the read direction is the one that
-- is easy to leave out:
--
--   * on the way in, `parse_content` refuses an unknown node type, an unknown
--     mark, an unknown attribute key, a tree deeper or wider than the bounds
--     in that module, and any URL scheme outside http/https/mailto;
--   * on the way OUT, `DocumentRepository` runs the same parser over the row
--     it just read, so a document written by a hand-run UPDATE, a bulk import,
--     a restored backup or some future second writer is refused at the
--     repository rather than rendered by a browser.
--
-- The schema is the floor beneath that, not a substitute for it -- the same
-- arrangement 019 describes for `saved_views.filter`, and the same one 007
-- describes for `labels_name_length`. `jsonb_typeof(content) = 'object'` is
-- what lets the parser start from "this is a mapping" instead of wondering
-- whether it was handed an array, a number or a bare JSON null; the length
-- CHECK is what stops one row being used to store a file.
--
-- `length(content::text)` and not `pg_column_size(content)`: the latter is
-- STABLE rather than IMMUTABLE and PostgreSQL refuses it in a CHECK outright.
-- The cast renders jsonb's CANONICAL text -- keys deduplicated, whitespace
-- normalised -- so the bound is on what is stored rather than on how prettily
-- the client happened to print it, and a megabyte of indentation cannot be
-- used to smuggle a small document past a large limit or vice versa.
--
-- ------------------------------------------------------------------
-- Where a document lives
-- ------------------------------------------------------------------
--
-- Two nullable target columns, `project_id` and `initiative_id`, with a CHECK
-- allowing at most one -- and both NULL meaning a workspace-level document,
-- which is an ordinary state and not a gap. This is `favorites` from 019 with
-- one fewer target, and it is that table's argument verbatim: a polymorphic
-- (parent_type, parent_id) pair cannot be the referencing half of a composite
-- foreign key, so a shared pair would have to carry no foreign key at all --
-- which is precisely the cross-tenant row every table in this file is shaped
-- to refuse. Two typed columns cost two constraints and two indexes and buy a
-- schema in which "a document on another workspace's project" is not a row.
--
-- ------------------------------------------------------------------
-- Version history: what a revision IS, and when one is taken
-- ------------------------------------------------------------------
--
-- A `document_revisions` row holds the content as it was BEFORE the edit that
-- superseded it. `documents` holds the current version and nothing else; the
-- history is the versions that have stopped being current. The alternative --
-- snapshotting the state AFTER each edit -- makes the newest revision a
-- duplicate of the live row, so every read has to know which of the two copies
-- is authoritative, and the answer differs for a document nobody has edited
-- yet. `created_at` on a revision is therefore the moment that version STOPPED
-- being the document, which is also the moment the next version started.
--
-- Restoring is an ordinary edit, not a rewind: `DocumentService.restore` takes
-- the chosen revision's content, snapshots the CURRENT content first, and then
-- writes. So restoring is itself undoable and no version is ever destroyed by
-- a restore. Nothing in this file mutates or deletes a revision, and there is
-- no `updated_at` on the table -- the same append-only-by-shape argument 022
-- makes about `project_updates`.
--
-- WHEN a revision is taken is the interesting question, and it is a product
-- rule, so it lives in `DocumentService.edit` and not in a trigger. The rule
-- and its reasoning:
--
--   * Never per keystroke. An editor that autosaves every few seconds would
--     otherwise write a revision per keystroke burst, and a history with four
--     hundred entries for one afternoon is a history nobody opens. The whole
--     point of a boundary is that the list of them is short enough to read.
--
--   * On an AUTHOR CHANGE -- the incoming editor is not `last_edited_by`.
--     This is the boundary that matters most and the one a time rule alone
--     misses. Without it, Ana's paragraph and Ben's deletion of it coalesce
--     into one version attributed to Ben, and the thing everybody actually
--     wants from history -- "what did it say before they touched it" -- is
--     gone. Handing over is a boundary whatever the clock says.
--
--   * On a TIME GAP -- the document has not been edited for
--     `app.domain.documents.REVISION_GAP`. One person's continuous session is
--     one version; coming back after lunch starts another. A gap is the only
--     signal available that a train of thought ended, and it is the one users
--     already have an intuition for, because it is what every editor they have
--     used does.
--
--   * On an EXPLICIT SAVE -- the client asked for one. An author who has just
--     finished something knows it is a boundary better than any heuristic
--     does, and refusing to record that would make the heuristic the ceiling
--     rather than the default.
--
--   * Never on a no-op. An edit that changes neither the title nor the content
--     writes nothing at all -- no revision, and no `updated_at` stamp. A
--     document marked as modified because a client re-sent what it already had
--     is a lie that propagates into every "recently changed" list built on
--     that column.
--
-- Deciding this requires READING `documents.updated_at` and `last_edited_by`
-- and then WRITING both, which is two statements with a window between them:
-- two concurrent edits could each read the same "last edited by Ana ten
-- minutes ago" and each decide no snapshot is needed, and one version would
-- vanish. `DocumentRepository.lock_for_edit` closes that with `SELECT ... FOR
-- UPDATE` on the document row inside the service's transaction -- a row lock
-- and not an advisory one, because unlike the cycle walks in 010 and 022 the
-- row that has to be locked is known before the read starts.
--
-- ------------------------------------------------------------------
-- Comments: a third table rather than a nullable column on `comments`
-- ------------------------------------------------------------------
--
-- `document_comments` is `comments` from 007 with `issues` swapped for
-- `documents`, deliberately down to the column list, the body bound and the
-- ascending index -- so a reader who knows one knows the other.
--
-- The tempting alternative is to make `comments.issue_id` nullable and add a
-- `document_id` beside it. It is refused for 022's reason, which is the reason
-- this whole schema keeps splitting tables: a row that may point at either of
-- two parents cannot carry a composite foreign key to both, so the shared
-- table would need `MATCH SIMPLE` keys and a CHECK, and every existing
-- guarantee about `comments` would have to be re-argued. It would also make
-- `issue_id` nullable on a table where NOT NULL is currently what makes a
-- comment about something.
--
-- What DOES differ from 007 is the referential action. `comments_issue_fk` is
-- ON DELETE CASCADE, argued there on the grounds that a comment on a deleted
-- issue is unreachable by construction. Every foreign key in this file is ON
-- DELETE RESTRICT instead, following 022: deleting a document destroys its
-- history and its discussion, which is a decision worth writing out in a
-- service where it can be read and changed -- `DocumentService.delete` clears
-- the comments and the revisions in the same transaction, in that order -- and
-- not one to leave as a clause that makes `DELETE FROM documents` silently
-- rewrite two other tables while reporting `DELETE 1`.
--
-- ------------------------------------------------------------------
--
-- Apply this ONLY through `python -m scripts.apply_migration
-- migrations/023_documents.sql`. The runner wraps the file in one transaction,
-- takes an advisory lock and records a checksum; a hand-run gets none of that
-- and executes statement-at-a-time in autocommit, which here means
-- `document_revisions` and `document_comments` referencing a `documents` that
-- may have no unique key for them to point at.
--
-- DEPENDS ON 002 (workspaces), 004 (workspace_members and the
-- (workspace_id, user_id) PRIMARY KEY every author reference below targets),
-- 009 (projects and projects_workspace_id_key) and 022 (initiatives and
-- initiatives_workspace_id_key). Applying this file before any of them fails
-- on the first constraint that needs it and -- inside the single transaction
-- the runner wraps the file in -- leaves nothing behind. That failure IS the
-- dependency check: the ledger records what has been applied and not what
-- depends on what, so the schema itself is what has to refuse an out-of-order
-- apply.


CREATE TABLE documents (
    id UUID PRIMARY KEY DEFAULT uuidv7(),

    -- One workspace_id for the whole row, read by every foreign key below.
    -- That is the entire cross-tenant guarantee: the project, the initiative,
    -- the creator and the last editor are all checked against the SAME tenant
    -- rather than against several that happen to be spelled separately.
    workspace_id UUID NOT NULL,

    -- Where this document lives. At most one is set; both NULL is a
    -- workspace-level document. See documents_one_parent below.
    --
    -- No DEFAULT on either, for 002's reason about the tenancy columns: a
    -- default outlives the migration, so every later insert that never
    -- mentioned a parent would silently file itself under whichever row the
    -- default named.
    project_id UUID,
    initiative_id UUID,

    title TEXT NOT NULL,

    -- The ProseMirror/TipTap tree. See the long block at the top of this file
    -- for why this is JSON and not HTML, and for the division of labour
    -- between the two CHECKs below and
    -- `app.domain.documents.parse_content`.
    --
    -- No DEFAULT. An empty document is `{"type":"doc","content":[]}`, which
    -- the parser accepts and which says "somebody made a document and has not
    -- written in it yet". A column default would let an INSERT that forgot the
    -- column mean the same thing by omission, and "empty" is not a state to
    -- arrive at by accident.
    content JSONB NOT NULL,

    -- Who created it, and who last changed it.
    --
    -- Both NOT NULL, matching `comments.author_id` in 007 and for its reason:
    -- a document is somebody's writing, and an unattributed one is not a
    -- record of anything. `last_edited_by` equals `creator_id` on insert
    -- rather than being nullable -- "nobody has edited it since it was made"
    -- is not a distinct fact from "the creator wrote what is there", and a
    -- NULL here would have to be resolved to the creator by every reader.
    --
    -- It is also half of the revision boundary rule: an incoming editor who is
    -- not this person forces a snapshot. A NULL would make that comparison
    -- true for everybody, which is the wrong answer in the safe direction --
    -- and being wrong in the safe direction on every edit is how a history
    -- turns back into one row per keystroke burst.
    creator_id UUID NOT NULL,
    last_edited_by UUID NOT NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- The moment the current version became current, which is what the time
    -- half of the revision boundary is measured from. Stamped by the
    -- application, as everywhere else in this schema -- see the note at the
    -- top of 007 about why there is no trigger.
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- RESTRICT on both sides, as 002 argues at teams_workspace_fk: a workspace
    -- with live documents is not something to delete by accident, and
    -- relocating a workspace's id is not something to do silently to the
    -- documents hanging off it.
    CONSTRAINT documents_workspace_fk
        FOREIGN KEY (workspace_id)
        REFERENCES workspaces (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    -- The project must be in THIS document's workspace.
    --
    -- `REFERENCES projects (id)` is the obvious spelling and it is the bug,
    -- for the reason 009 gives at projects_lead_fk and 019 repeats at
    -- saved_views_team_fk: it would check that the project is a real project
    -- somewhere and say nothing about where, so any project id from any tenant
    -- would collect any workspace's documents. The reference is composite onto
    -- the pair projects_workspace_id_key marks [FK TARGET] in 009, and one
    -- workspace_id column feeds it and documents_workspace_fk alike.
    --
    -- MATCH SIMPLE (the default) skips this check entirely for a row with any
    -- NULL among its referencing columns. That is the wanted behaviour and it
    -- is only safe because `workspace_id` is NOT NULL: the sole column that
    -- can be NULL here is project_id, so the exemption is precisely "this
    -- document is not about a project" and cannot be widened by a NULL
    -- arriving in the other half.
    CONSTRAINT documents_project_fk
        FOREIGN KEY (workspace_id, project_id)
        REFERENCES projects (workspace_id, id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    -- The same sentence about initiatives, onto the pair
    -- initiatives_workspace_id_key marks [FK TARGET] in 022.
    CONSTRAINT documents_initiative_fk
        FOREIGN KEY (workspace_id, initiative_id)
        REFERENCES initiatives (workspace_id, id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    -- The creator and the last editor must both be members of THIS document's
    -- workspace.
    --
    -- Composite against `workspace_members`, NOT single-column against
    -- `users`, and 007 spells out at comments_author_fk exactly what the
    -- difference buys: a plain `REFERENCES users (id)` is satisfied by every
    -- account in the installation, so any user anywhere could be recorded as
    -- having written any workspace's document -- an impersonation the database
    -- would accept without a word. 004 made (workspace_id, user_id) the
    -- PRIMARY KEY of workspace_members precisely so this reference is
    -- declarable.
    --
    -- ON DELETE RESTRICT: a member who has written or edited a document cannot
    -- be removed from the workspace until those documents are reassigned or
    -- deleted. That is the correct refusal to make loudly; CASCADE would
    -- destroy a workspace's written record as a side effect of an
    -- administrative removal.
    CONSTRAINT documents_creator_fk
        FOREIGN KEY (workspace_id, creator_id)
        REFERENCES workspace_members (workspace_id, user_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    CONSTRAINT documents_last_editor_fk
        FOREIGN KEY (workspace_id, last_edited_by)
        REFERENCES workspace_members (workspace_id, user_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    -- A document hangs off at most one thing. `<= 1` and not `= 1`, unlike
    -- favorites_one_target in 019, and the difference is a product decision
    -- rather than an oversight: a favourite pointing at nothing is a position
    -- in a list with nothing at it, whereas a document belonging to no project
    -- and no initiative is an ordinary workspace-level document -- the
    -- handbook, the onboarding guide -- and is probably the common case.
    --
    -- What it refuses is a document claiming both, which is one row that has
    -- to appear in two places and be moved twice.
    CONSTRAINT documents_one_parent
        CHECK (num_nonnulls(project_id, initiative_id) <= 1),

    -- The floor beneath app/services/documents.py, as labels_name_length is
    -- beneath the label rules. 200 is the title bound projects, milestones,
    -- initiatives and saved views already use; a fifth spelling of the same
    -- number would be a number to get wrong.
    CONSTRAINT documents_title_length
        CHECK (char_length(title) BETWEEN 1 AND 200),

    -- The content is an OBJECT, so the parser never has to consider an array,
    -- a number, a string or a bare JSON null. Those are not documents anyone
    -- can author through the API; they are what a hand-written INSERT or a
    -- future second writer would produce, and this is where that is caught --
    -- before the row exists, rather than on the read that would have to fail
    -- in front of a reader.
    CONSTRAINT documents_content_is_object
        CHECK (jsonb_typeof(content) = 'object'),

    -- A bound, not a validation -- the same distinction 022 draws on
    -- project_updates_body_length. What the content MEANS is
    -- `app.domain.documents.parse_content`'s question; this is only the
    -- ceiling that stops one row being megabytes wide, which matters more here
    -- than for a comment because every revision copies it.
    --
    -- 524288 is 512 KiB, and it is deliberately MORE THAN TWICE the 200,000
    -- characters `app.domain.documents.MAX_CONTENT_CHARACTERS` allows. The gap
    -- is arithmetic rather than caution: PostgreSQL's jsonb text output puts a
    -- space after every `:` and every `,` while the JSON the application
    -- renders has neither, so the string this CHECK measures is longer than
    -- the one the service measured. A ceiling set equal to the service's bound
    -- would let a document pass validation and then fail this constraint --
    -- surfacing as a CheckViolationError on the INSERT, which is a masked
    -- internal error rather than the field error its author can act on.
    --
    -- The expansion cannot exceed one character per `:` plus one per `,`, and
    -- in this renderer a `:` costs at least four characters (`"k":`) while a
    -- `,` costs at least one more beside it -- so the stored string is under
    -- 1.75x what the service counted. Twice is the round number above that,
    -- and tests/test_documents.py pins the pair rather than leaving them to
    -- agree by habit.
    CONSTRAINT documents_content_length
        CHECK (length(content::text) <= 524288),

    -- [FK TARGET] Redundant beside the primary key on id only if you do not
    -- look at what points here. A foreign key may reference a UNIQUE column
    -- SET and nothing else, and `(workspace_id, id)` is the pair
    -- document_revisions_document_fk and document_comments_document_fk both
    -- reference. Without it neither can be declared at all, and the schema
    -- falls back to single-column keys that permit exactly the cross-workspace
    -- rows this migration exists to refuse.
    CONSTRAINT documents_workspace_id_key UNIQUE (workspace_id, id)
);


-- One superseded version of one document.
--
-- Append-only by shape rather than by permission, as `project_updates` in 022:
-- there is no `updated_at` and no edit path, because a revision is what the
-- document said at a moment and rewriting it would rewrite the history the
-- live row is the continuation of. Nothing deletes a revision either, except
-- `DocumentService.delete` clearing them on the way to deleting the document
-- they are versions of.
CREATE TABLE document_revisions (
    id UUID PRIMARY KEY DEFAULT uuidv7(),

    -- Denormalised from the document solely so that the two foreign keys below
    -- can be composite, and therefore so a read can be scoped by tenant
    -- without joining `documents`. The value is not independent -- the
    -- document key forces it to equal the document's own workspace -- so it
    -- cannot drift. The same argument 007 makes about `comments.workspace_id`.
    workspace_id UUID NOT NULL,
    document_id UUID NOT NULL,

    -- The title AND the content as they were, not just the content. A renamed
    -- document whose history showed only bodies would restore to the right
    -- text under the wrong name, and "what was this called before" is a
    -- question the history is asked as often as "what did it say".
    title TEXT NOT NULL,
    content JSONB NOT NULL,

    -- Who wrote the version this row holds -- NOT who took the snapshot. The
    -- snapshot is taken by the NEXT editor, so recording them here would
    -- attribute every version to the person who replaced it, which is the
    -- exact opposite of what a history is for. `DocumentService.edit` copies
    -- `documents.last_edited_by` into this column, and that column's own
    -- foreign key has already established the person is a member here.
    author_id UUID NOT NULL,

    -- When this version stopped being current, which is the same instant the
    -- next version started. Named `created_at` rather than `superseded_at`
    -- because it is when the ROW was created and because every keyset walk in
    -- this schema is written against that name; the distinction is worth
    -- knowing when reading a history, not worth a column name nothing else
    -- shares.
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- ON DELETE RESTRICT rather than CASCADE. See the note at the top of this
    -- file: destroying a document's history is a decision
    -- `DocumentService.delete` makes out loud in one transaction, not a side
    -- effect of a DELETE that names a different table.
    CONSTRAINT document_revisions_document_fk
        FOREIGN KEY (workspace_id, document_id)
        REFERENCES documents (workspace_id, id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    CONSTRAINT document_revisions_author_fk
        FOREIGN KEY (workspace_id, author_id)
        REFERENCES workspace_members (workspace_id, user_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    -- The same three constraints `documents` puts on the same two columns,
    -- written out again rather than shared. There is no way to share a CHECK
    -- between two tables short of a domain type or a function, and both put
    -- the rule somewhere a reader of either table cannot see it -- 022 makes
    -- this call about the health vocabulary and tests/test_migration_023_db.py
    -- asserts the pairs agree, as tests/test_migration_022_db.py does there.
    --
    -- They matter as much here as on the live row: a revision is restored back
    -- INTO `documents`, so a revision the live table would refuse is a history
    -- entry nobody can ever restore.
    CONSTRAINT document_revisions_title_length
        CHECK (char_length(title) BETWEEN 1 AND 200),

    CONSTRAINT document_revisions_content_is_object
        CHECK (jsonb_typeof(content) = 'object'),

    CONSTRAINT document_revisions_content_length
        CHECK (length(content::text) <= 524288)
);


-- Discussion on a document.
--
-- `comments` from 007 with `issues` swapped for `documents`, deliberately down
-- to the column list and the body bound; see the block at the top of this file
-- for why it is a second table rather than a nullable column on that one, and
-- for why its foreign key is RESTRICT where 007's is CASCADE.
--
-- Document-LEVEL discussion only. There is no anchor into the content and
-- deliberately so: an inline comment has to point at a range of a tree that
-- the next edit reshapes, which needs a position-mapping scheme -- marks
-- carrying comment ids, rebased on every edit -- that is a feature with its
-- own migration and its own failure modes. A `block_id` column added here
-- speculatively would be an anchor nothing maintains, pointing at nodes that
-- have moved.
CREATE TABLE document_comments (
    id UUID PRIMARY KEY DEFAULT uuidv7(),

    workspace_id UUID NOT NULL,
    document_id UUID NOT NULL,

    author_id UUID NOT NULL,

    body TEXT NOT NULL,

    -- NULL until the comment is edited, and NULL is the claim "never edited" --
    -- 007's column, with 007's meaning and 007's distinction from updated_at,
    -- which any write touches. No mutation sets it yet, and it exists now
    -- because the alternative is adding it later to a populated table where
    -- every existing comment would have to be assigned an edit history it does
    -- not have.
    edited_at TIMESTAMPTZ,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT document_comments_document_fk
        FOREIGN KEY (workspace_id, document_id)
        REFERENCES documents (workspace_id, id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    -- 007's comments_author_fk, unchanged and for its reasons: composite
    -- against `workspace_members` so that a comment cannot be attributed to
    -- somebody who is not here, and RESTRICT so that removing a member who has
    -- commented is refused rather than silently destroying a discussion.
    CONSTRAINT document_comments_author_fk
        FOREIGN KEY (workspace_id, author_id)
        REFERENCES workspace_members (workspace_id, user_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    -- 007's bound, to the byte. An empty body is not a comment; the ceiling is
    -- generous enough that no real comment meets it and small enough that a
    -- single row cannot be used to store a file.
    CONSTRAINT document_comments_body_length
        CHECK (length(body) BETWEEN 1 AND 16384)
);


-- The product list: one workspace's documents, newest first. The keyset shape
-- 002 uses for issues and 009 for projects. `workspace_id` leads because every
-- product query does; (id DESC) breaks a created_at tie, which is what keeps
-- the cursor comparison in DocumentRepository.list total and therefore stable.
CREATE INDEX documents_workspace_created_at_id_idx
    ON documents (workspace_id, created_at DESC, id DESC);

-- "This project's documents", and the referencing side of
-- documents_project_fk.
--
-- PostgreSQL indexes the REFERENCED side of a foreign key and never the
-- referencing side, so without this every deletion from `projects` scans
-- `documents` in full to satisfy the RESTRICT above -- on the path of every
-- project deletion in every workspace.
--
-- (created_at DESC, id DESC) trails the equality so that the same index serves
-- the filtered list in the same order the unfiltered one uses. NOT partial on
-- `project_id IS NOT NULL`, though many documents will have no project, for
-- the reason 009 gives and 019 and 022 repeat: the predicate a
-- referential-integrity check issues is generated by PostgreSQL rather than
-- written here, so whether it matches a partial index is a property of the
-- planner's implication prover rather than of this schema, and the cost of
-- being wrong is a full scan on every project deletion.
CREATE INDEX documents_workspace_project_created_at_id_idx
    ON documents (workspace_id, project_id, created_at DESC, id DESC);

-- The same, for initiatives.
CREATE INDEX documents_workspace_initiative_created_at_id_idx
    ON documents (workspace_id, initiative_id, created_at DESC, id DESC);

-- The referencing sides of documents_creator_fk and documents_last_editor_fk.
-- Two indexes because they are two columns: an index on one does not serve a
-- referential check against the other, so without both, removing a member
-- scans `documents` in full. They also answer "everything this person wrote
-- here" and "everything they last touched", which is the query a member-removal
-- path has to run before it can offer any choice at all.
CREATE INDEX documents_workspace_creator_idx
    ON documents (workspace_id, creator_id);

CREATE INDEX documents_workspace_last_editor_idx
    ON documents (workspace_id, last_edited_by);

-- One document's history, newest first -- which is both the product read and
-- the lookup a restore resolves a revision id against -- and the referencing
-- side of document_revisions_document_fk in the same index. `id DESC` is the
-- tie-break that makes the order total, which matters more here than usual:
-- two revisions written in one transaction share a `created_at`, and "which
-- version came first" must not depend on the scan order.
CREATE INDEX document_revisions_workspace_document_created_at_id_idx
    ON document_revisions (workspace_id, document_id, created_at DESC, id DESC);

-- The referencing side of document_revisions_author_fk. Without it every
-- member removal scans `document_revisions` in full.
CREATE INDEX document_revisions_workspace_author_idx
    ON document_revisions (workspace_id, author_id);

-- The thread read, in the order it is rendered: one document's comments,
-- oldest first. All four columns are load-bearing -- (workspace_id,
-- document_id) is the equality, and (created_at, id) is the keyset the cursor
-- compares against, with id breaking a created_at tie so the ordering is total
-- and a page walk cannot repeat or skip a row. Ascending, like 007's index on
-- `comments` and unlike everything else here, because a discussion reads
-- forwards.
CREATE INDEX document_comments_workspace_document_created_idx
    ON document_comments (workspace_id, document_id, created_at, id);

-- The referencing side of document_comments_author_fk, exactly as
-- comments_workspace_author_idx is 007's.
CREATE INDEX document_comments_workspace_author_idx
    ON document_comments (workspace_id, author_id);
