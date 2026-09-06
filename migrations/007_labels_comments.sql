-- Labels, the issue<->label join, and comments.
--
-- Three tables, and nothing altered on an existing one.
--
-- The shape every table here repeats is 002's, and for 002's reason: a row
-- that names two tenant-owned things must name them through ONE workspace_id
-- column, so that a single value has to satisfy both parents. Two
-- single-column foreign keys would each pass while together describing an
-- issue in workspace A wearing a label from workspace B. That is not a check
-- the application is trusted to perform here -- there is no SELECT before any
-- insert below, deliberately, because a separate statement can be raced and
-- two places that have to agree eventually will not.
--
-- Apply this ONLY through `python -m scripts.apply_migration
-- migrations/007_labels_comments.sql`. See the note at the top of
-- migrations/002_tenancy.sql for what a hand-run costs: no ledger row, no
-- advisory lock, no recorded checksum, and one statement at a time in
-- autocommit rather than the single transaction that makes a failure
-- anywhere leave the schema exactly as it was.
--
-- DEPENDENCIES. Two, and neither is declared here:
--
--   * `workspace_members`, from migrations/004_membership.sql.
--     `comments (workspace_id, author_id)` references it.
--   * `issues_workspace_id_key`, the UNIQUE (workspace_id, id) on `issues`,
--     from migrations/006_issue_fields.sql. `issue_labels_issue_fk` and
--     `comments_issue_fk` reference that pair, and PostgreSQL will only let a
--     foreign key target a uniquely-constrained column SET -- so without it
--     neither join below can be tenant-pinned to an issue at all, and both
--     would have to fall back to a single-column reference that says nothing
--     about tenancy.
--
-- 006 declares it rather than this file, and the reason is mechanical rather
-- than architectural: a constraint name must be unique, so two migrations
-- declaring the same one means the second to RUN fails on the duplicate --
-- and that failure lands on whichever branch merged second rather than on
-- whoever wrote it. 006 is the lowest-numbered pending migration, which makes
-- it the only one every other file can depend on. Anything else needing that
-- pair references it; nothing else creates it.
--
-- Applying this file before either dependency fails on the constraint that
-- needs it and, inside the single transaction the runner wraps this file in,
-- leaves nothing behind. That failure IS the dependency check: the ledger
-- records what has been applied and not what depends on what, so the schema
-- itself is what refuses an out-of-order apply.
--
-- There is no updated_at trigger, here or anywhere in this schema. A PL/pgSQL
-- body is dollar-quoted and tests/test_migration_lint.py rule 1 reads the
-- BEGIN that opens the block as transaction control (hole #4, deferred
-- 2026-09-02, with an xfail test naming it). Until that rule is fixed the
-- application owns every updated_at column, which is the arrangement `issues`
-- has lived under since 001.


-- Workspace-scoped rather than team-scoped: a label is a taxonomy that cuts
-- across teams -- the same "regression" applies to work in every one of them
-- -- so team-scoping would force a duplicate label per team and would make
-- the join below pin two keys instead of one for no isolation the workspace
-- key does not already provide.
CREATE TABLE labels (
    id UUID PRIMARY KEY DEFAULT uuidv7(),

    workspace_id UUID NOT NULL,

    name TEXT NOT NULL,

    -- '#rrggbb', lowercase. Not nullable and given no database default: a
    -- default here would outlive the migration and quietly colour every
    -- label whose creator forgot to choose, which is 002's argument about
    -- issues.workspace_id applied to a far less important column. The
    -- application's default lives in app/services/labels.py, where it is one
    -- named constant a reader can find.
    color TEXT NOT NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- RESTRICT on both sides, as 002 argues at teams_workspace_fk: a
    -- workspace with live labels is not something to delete by accident, and
    -- relocating a workspace's id is not something to do silently to the
    -- labels that hang off it.
    CONSTRAINT labels_workspace_fk
        FOREIGN KEY (workspace_id)
        REFERENCES workspaces (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    -- [FK TARGET] What issue_labels (workspace_id, label_id) points at.
    --
    -- Redundant as a uniqueness CLAIM -- `id` is already the primary key, so
    -- the pair cannot repeat -- and not redundant as a constraint: a foreign
    -- key may only target a uniquely-constrained column SET, so without this
    -- the composite key on the join below could not be declared at all. This
    -- is the counterpart, on a table this file owns, of the
    -- `issues_workspace_id_key` that 006 declares.
    CONSTRAINT labels_workspace_id_key UNIQUE (workspace_id, id),

    -- The floor beneath app/services/labels.py, not a replacement for it. The
    -- service checks the same two numbers and reports a structured field
    -- error; this makes a row that skipped the service -- a fixture, a
    -- console, a future repository method -- impossible rather than merely
    -- unexpected.
    CONSTRAINT labels_name_length CHECK (length(name) BETWEEN 1 AND 50),

    -- Lowercase hex, exactly six digits. The character class is what enforces
    -- the folding: '#FFFFFF' does not match, so the canonical spelling of a
    -- colour is the stored one and two rows cannot hold the same colour in
    -- two spellings. The service folds on the way in, which is the same
    -- arrangement 003 gives users.email and 002 gives workspaces.slug.
    CONSTRAINT labels_color_format CHECK (color ~ '^#[0-9a-f]{6}$')
);


-- Label names are unique per workspace, case-insensitively.
--
-- An index rather than a table constraint, because a UNIQUE constraint cannot
-- be declared over an expression. The uniqueness is the product rule: a label
-- picker showing both "Bug" and "bug" offers a choice with no meaning, and
-- whichever the author picks the other one still collects issues. A plain
-- UNIQUE (workspace_id, name) over TEXT is case-SENSITIVE and would admit
-- exactly that pair.
--
-- lower() rather than folding the stored value the way 003 folds an email,
-- because a label name is displayed back and its capitalisation is the
-- author's. Uniqueness is case-insensitive; the spelling is not rewritten.
--
-- lower() is IMMUTABLE, which is what makes it legal in an index at all.
--
-- The name ends in _key rather than _idx because PostgreSQL reports the index
-- name as the constraint name in a unique-violation error, and
-- app/services/labels.py matches on it to tell a duplicate name from any
-- other write failure.
CREATE UNIQUE INDEX labels_workspace_name_key ON labels (workspace_id, lower(name));

-- The read order: one workspace's labels, alphabetically. A second index over
-- almost the same thing, and not a duplicate of the one above -- that one is
-- over lower(name) and can only answer a query written in terms of lower(name),
-- which the listing deliberately is not.
--
-- The listing orders by the raw column so that a keyset cursor carries the
-- stored value back verbatim and PostgreSQL compares it under the same
-- collation that produced the ordering. A cursor holding lower(name) would
-- have to be lowercased by whoever minted it, and Python's str.lower() and
-- PostgreSQL's lower() do not agree on every Unicode string -- a disagreement
-- that surfaces as a page walk that skips or repeats a row, on exactly the
-- names nobody tests with.
--
-- (id) breaks a name tie. Ties are already impossible -- two labels sharing a
-- name would share a lower(name) and collide on the unique index above -- so
-- this is what keeps the ordering total if that rule is ever relaxed, not a
-- claim that it is needed today.
CREATE INDEX labels_workspace_name_idx ON labels (workspace_id, name, id);


-- The join. One workspace_id column serving both foreign keys is the whole
-- mechanism: an issue from workspace A and a label from workspace B have no
-- single value of workspace_id that satisfies both parents, so the server
-- refuses the row. Nothing in the application has to remember to check.
CREATE TABLE issue_labels (
    workspace_id UUID NOT NULL,
    issue_id UUID NOT NULL,
    label_id UUID NOT NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- The natural key IS the row; there is nothing else here to identify. A
    -- surrogate id would need a UNIQUE (issue_id, label_id) beside it to stop
    -- a label being applied twice, which is this key with an extra column and
    -- an extra index paying for it.
    --
    -- workspace_id leads, so the primary key's index is also the one that
    -- answers "this issue's labels" -- the query Issue.labels runs -- and the
    -- one the referential check behind issue_labels_issue_fk uses. Every
    -- index in this schema leads with the tenant for the same reason: a
    -- lookup that does not begin with workspace_id can only be served by
    -- scanning at a cost proportional to the global row count.
    CONSTRAINT issue_labels_pkey PRIMARY KEY (workspace_id, issue_id, label_id),

    -- ON DELETE CASCADE on both, which is a deliberate departure from 002's
    -- RESTRICT-everywhere posture and rests on what the row is. An
    -- issue_labels row carries no data of its own: it is an association, and
    -- destroying one loses nothing that re-attaching cannot restore. 002
    -- protects `issues`, which ARE the data, and there a single DELETE under
    -- CASCADE would take the product's contents with it.
    --
    -- RESTRICT here would mean a label could not be deleted while any issue
    -- wore it, which is not a rule anyone wants: deleting a label is exactly
    -- how you stop using it.
    --
    -- ON UPDATE RESTRICT on both, unchanged from 002: relocating an issue's
    -- or a label's key must not silently rewrite the rows pointing at it.
    CONSTRAINT issue_labels_issue_fk
        FOREIGN KEY (workspace_id, issue_id)
        REFERENCES issues (workspace_id, id)
        ON DELETE CASCADE ON UPDATE RESTRICT,

    CONSTRAINT issue_labels_label_fk
        FOREIGN KEY (workspace_id, label_id)
        REFERENCES labels (workspace_id, id)
        ON DELETE CASCADE ON UPDATE RESTRICT
);

-- PostgreSQL indexes the REFERENCED side of a foreign key, never the
-- REFERENCING side, so without this every label deletion has to scan
-- issue_labels in full to find the rows its CASCADE must remove. The column
-- order is the foreign key's own, because that is the lookup the referential
-- action performs. It also answers "which issues wear this label".
CREATE INDEX issue_labels_workspace_label_idx ON issue_labels (workspace_id, label_id);


-- Discussion on an issue. Deliberately NOT an activity or audit log: a
-- comment is something a person wrote and may delete, and a durable history
-- of what happened to an issue is a different table with an explicit event
-- type and payload, which this migration does not create. Reconstructing
-- activity from these rows, or from any updated_at, would be inventing a
-- history nobody recorded.
CREATE TABLE comments (
    id UUID PRIMARY KEY DEFAULT uuidv7(),

    -- Denormalised from the issue solely so that comments_issue_fk can be
    -- composite, and therefore so that a read can be scoped by tenant without
    -- joining issues. The value is not independent -- the foreign key forces
    -- it to equal the issue's own workspace -- so it cannot drift.
    workspace_id UUID NOT NULL,
    issue_id UUID NOT NULL,

    author_id UUID NOT NULL,

    body TEXT NOT NULL,

    -- NULL until the comment is edited, and NULL is the claim "never edited".
    -- Distinct from updated_at, which any write touches: a row rewritten by a
    -- migration or a moderation action has a new updated_at and has not been
    -- edited by its author. No mutation sets this yet -- commentUpdate is not
    -- in this phase -- and the column exists now because the alternative is
    -- adding it later to a populated table, where every existing comment
    -- would have to be assigned an edit history it does not have.
    edited_at TIMESTAMPTZ,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- The composite key that makes a cross-tenant comment unrepresentable: a
    -- comment claiming workspace A on an issue owned by B has no workspace_id
    -- that satisfies this reference.
    --
    -- ON DELETE CASCADE, unlike 002's RESTRICT on issues. Deleting an issue
    -- deletes its discussion because a comment on a deleted issue is
    -- unreachable by construction -- there is no query that could ever return
    -- it -- so RESTRICT would only make issue deletion impossible while
    -- preserving rows nobody can read.
    CONSTRAINT comments_issue_fk
        FOREIGN KEY (workspace_id, issue_id)
        REFERENCES issues (workspace_id, id)
        ON DELETE CASCADE ON UPDATE RESTRICT,

    -- Composite against `workspace_members`, NOT single-column against
    -- `users`, and the difference is the whole tenancy property. A plain
    -- `REFERENCES users (id)` is satisfied by every account in the
    -- installation, so any user anywhere could be recorded as the author of a
    -- comment in any workspace -- an impersonation the database would accept
    -- without a word, leaving "is this author actually a member here?" as a
    -- rule the application has to remember on every write. 004 made
    -- (workspace_id, user_id) the PRIMARY KEY of workspace_members precisely
    -- so this reference is declarable; because it is pinned to the SAME
    -- workspace_id column as comments_issue_fk above, one value has to satisfy
    -- both parents and a cross-tenant author has no row at all.
    --
    -- ON DELETE RESTRICT, which is a real product consequence and not a
    -- default: a member who has commented cannot be removed from the workspace
    -- until their comments are reassigned, anonymised or deleted. That is the
    -- correct refusal to make loudly. The alternative, CASCADE, would destroy a
    -- discussion as a side effect of an administrative removal, and the
    -- alternative to the constraint is the impersonation above. Whoever writes
    -- the "remove a member" path owns that decision; this file will not make it
    -- silently. ON UPDATE RESTRICT for 002's reason: relocating a membership's
    -- key must not silently rewrite the rows pointing at it.
    CONSTRAINT comments_author_fk
        FOREIGN KEY (workspace_id, author_id)
        REFERENCES workspace_members (workspace_id, user_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    -- The floor beneath app/services/comments.py, as labels_name_length is
    -- beneath the label rules. An empty body is not a comment; the ceiling is
    -- generous enough that no real comment meets it and small enough that a
    -- single row cannot be used to store a file.
    CONSTRAINT comments_body_length CHECK (length(body) BETWEEN 1 AND 16384)
);

-- The thread read, in the order it is rendered: one issue's comments, oldest
-- first. All four columns are load-bearing -- (workspace_id, issue_id) is the
-- equality, and (created_at, id) is the keyset the cursor compares against,
-- with id breaking a created_at tie so the ordering is total and a page walk
-- cannot repeat or skip a row. Ascending, unlike the issues index, because a
-- discussion reads forwards.
CREATE INDEX comments_workspace_issue_created_idx
    ON comments (workspace_id, issue_id, created_at, id);

-- The referencing side of comments_author_fk. PostgreSQL indexes the
-- REFERENCED side of a foreign key and never the REFERENCING side, so without
-- this every removal from `workspace_members` scans comments in full to
-- evaluate the RESTRICT above. The column order is the foreign key's own,
-- because that is the lookup the referential action performs; it also answers
-- "everything this person wrote in this workspace", which is the query a
-- member-removal path has to run before it can offer any choice at all.
CREATE INDEX comments_workspace_author_idx ON comments (workspace_id, author_id);
