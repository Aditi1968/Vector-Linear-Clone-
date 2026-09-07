-- Triage, and label groups with optional mutual exclusion.
--
-- Two features in one file because they are two halves of the same product
-- moment: work arrives from somewhere nobody chose, and somebody has to say
-- what kind of thing it is before it can be worked on. Triage is the queue
-- that holds it; a label group is how the answer is recorded without letting
-- an issue claim two incompatible answers at once.
--
-- Neither half adds a table the other reads. They are one migration rather
-- than two only because they ship together; a reader looking for the triage
-- schema can stop at the first section and a reader looking for label groups
-- can start at the second.
--
-- Apply this ONLY through `python -m scripts.apply_migration
-- migrations/021_triage_label_groups.sql`. The runner wraps the file in one
-- transaction, takes an advisory lock and records a checksum; a hand-run gets
-- none of that and executes statement-at-a-time in autocommit, which here
-- would be the worst case this repository has yet had. The backfill of
-- `issue_labels.exclusivity_key` sits between an ADD COLUMN and a SET NOT
-- NULL: a run that stopped in the middle would leave the join table carrying
-- a nullable column with no constraint behind it and no record that anything
-- had been attempted.
--
-- DEPENDS ON 002, for `workspaces` and the tenancy columns on `issues`; on
-- 005, for `workflow_states` and the team-scoped board a declined issue is
-- moved onto; on 006, for `issues.archived_at`; and on 007, for `labels`,
-- `issue_labels` and `labels_workspace_id_key`. Applying this file before any
-- of them fails on the constraint that needs it and -- inside the single
-- transaction the runner wraps the file in -- leaves nothing behind. That
-- failure IS the dependency check: the ledger records what has been applied
-- and not what depends on what, so the schema itself is what has to refuse an
-- out-of-order apply.
--
-- There is no trigger here, and none is possible: a PL/pgSQL body is
-- dollar-quoted and tests/test_migration_lint.py rule 1 reads the BEGIN that
-- opens the block as transaction control (hole #4, deferred 2026-09-02). That
-- constraint is the reason the exclusivity rule below is expressed as a
-- generated column and a partial unique index rather than as the row-level
-- check a trigger would have written.


-- ======================================================================
-- Triage
-- ======================================================================
--
-- One nullable column, and the argument for its shape is worth setting out
-- because three other shapes are more obvious and all three are worse.
--
--   * A sixth `workflow_states.type`. This is what Linear does and it is the
--     shape this schema cannot afford: `workflow_states_type_check` is read
--     by TERMINAL_STATE_CATEGORIES, by WorkflowStateCategory, by the
--     state_category filter and by the completed_at rule, so widening the
--     vocabulary changes the meaning of `type` in every one of them. Triage
--     is not a status the work is IN; it is a queue the work is waiting in
--     BEFORE it has a status anyone chose.
--   * A `triage_issues` join table keyed on (workspace_id, issue_id). One
--     row per issue, holding one timestamp, joined on every read of the
--     queue -- a table whose primary key is a foreign key to another table's
--     primary key is a column that has been given its own file.
--   * A boolean. `in_triage BOOLEAN NOT NULL DEFAULT FALSE` says whether but
--     not since when, and "how long has this been sitting here" is the only
--     question anyone asks of an incoming queue. It would also need a
--     separate ordering key, which is a second column doing the job this one
--     already does.
--
-- So: an instant, or NULL. NULL means the issue is not in any triage queue,
-- which is what almost every issue is; a value means it is waiting in the
-- queue of the team named by `issues.team_id`, and has been since then. The
-- column is therefore both the flag and the ordering key, and the partial
-- index below is both the queue read and the whole of its cost.
--
-- No DEFAULT, for the reason migrations/002_tenancy.sql gives about the
-- tenancy columns: a default outlives the migration, and `DEFAULT now()`
-- would file every issue any future insert created into the triage queue of
-- whichever team it named. Entering triage is an act, and acts are performed
-- by statements that say so.
--
-- Nullable and staying nullable. No backfill and no SET NOT NULL follow, so
-- nothing here rewrites `issues` -- on PostgreSQL 11+ a nullable ADD COLUMN
-- with no default is a catalog change.
ALTER TABLE issues ADD COLUMN triage_entered_at TIMESTAMPTZ;

-- An archived issue is not waiting for anybody.
--
-- The read below carries `archived_at IS NULL` and would be enough on its
-- own; this makes the pair a property of the row rather than of every query
-- that remembers to ask. It is also the cheaper half of the rule: archiving
-- an issue that is still in triage is refused outright, so whoever adds the
-- first bulk-archive path has to decide what happens to the queue entry
-- instead of silently leaving a row nothing will ever read again.
--
-- Both columns are nullable, so this is written with two IS NULL tests rather
-- than an inequality: a CHECK treats NULL as satisfied, and `archived_at <>
-- triage_entered_at` would be NULL -- and therefore accepted -- for exactly
-- the rows it is meant to be about.
ALTER TABLE issues ADD CONSTRAINT issues_triage_is_not_archived
    CHECK (archived_at IS NULL OR triage_entered_at IS NULL);

-- The queue, and nothing else.
--
-- Partial on `triage_entered_at IS NOT NULL`, which is the one place in this
-- schema where a partial index is unambiguously right. 009 declines to make
-- projects_workspace_lead_idx partial because the predicate a foreign key's
-- RESTRICT check issues is generated by PostgreSQL rather than written here,
-- so whether it matches a partial index is a property of the planner's
-- implication prover. Nothing references this column, so there is no such
-- check: the only reader is the queue query in TriageRepository, which spells
-- `triage_entered_at IS NOT NULL` literally. The great majority of issues are
-- not in triage, so the index holds the queue and not the table.
--
-- (workspace_id, team_id) is the equality -- triage is per TEAM, which is the
-- whole product claim -- and (triage_entered_at, id) is the keyset. Ascending
-- both, because a queue is read oldest first: the thing that has been waiting
-- longest is the thing somebody has to look at. `id` breaks a tie so the
-- ordering is total, which is what keeps a page walk from repeating or
-- skipping a row when two issues enter triage in the same microsecond -- which
-- a bulk import does constantly.
CREATE INDEX issues_workspace_team_triage_idx
    ON issues (workspace_id, team_id, triage_entered_at, id)
    WHERE triage_entered_at IS NOT NULL;


-- ======================================================================
-- Label groups
-- ======================================================================
--
-- A parent for labels, and -- per group, optionally -- the rule that an issue
-- may wear at most one of the labels under it.
--
-- "Optionally, per group" is the entire difficulty. Exclusivity is a property
-- of the GROUP, membership is a property of the LABEL, and the rule has to be
-- enforced on rows of `issue_labels`, which is a third table that knows
-- neither. A CHECK constraint may not read another row and a unique index may
-- not join, so the rule cannot be written where it is about -- unless the two
-- facts it needs are carried down to it. The rest of this section is that
-- carrying, done in a way that makes the copies unable to disagree with their
-- sources rather than merely unlikely to.
CREATE TABLE label_groups (
    id UUID PRIMARY KEY DEFAULT uuidv7(),

    -- Workspace-scoped, exactly as `labels` is and for 007's reason: a
    -- taxonomy cuts across teams, so team-scoping would force a duplicate
    -- group per team and buy no isolation the workspace key does not already
    -- provide.
    workspace_id UUID NOT NULL,

    name TEXT NOT NULL,

    -- Whether an issue may wear more than one label from this group.
    --
    -- NOT NULL and given no database default. A default here would decide,
    -- silently and forever, what an unspecified group means -- and the two
    -- answers are not interchangeable: an exclusive group refuses writes a
    -- non-exclusive one accepts. Whoever creates a group says which kind it
    -- is. app/services/labels.py holds the application's default, in the one
    -- place a reader can find it, exactly as DEFAULT_COLOR is held for
    -- `labels.color`.
    --
    -- Mutable, and the two ON UPDATE CASCADE clauses below are what make it
    -- so. See labels_group_fk for what a flip does and what it costs.
    exclusive BOOLEAN NOT NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- RESTRICT on both sides, as 002 argues at teams_workspace_fk and 007
    -- repeats at labels_workspace_fk.
    CONSTRAINT label_groups_workspace_fk
        FOREIGN KEY (workspace_id)
        REFERENCES workspaces (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    -- The floor beneath app/services/labels.py, not a replacement for it.
    -- Matching labels_name_length, because a group name and a label name are
    -- rendered in the same picker and a rule that differed between them would
    -- be a rule nobody could state.
    CONSTRAINT label_groups_name_length CHECK (length(name) BETWEEN 1 AND 50),

    -- [FK TARGET] What labels_group_fk references, and the reason it carries
    -- three columns rather than the usual two.
    --
    -- `(workspace_id, id)` would be the ordinary pair and would only pin the
    -- tenant. Adding `exclusive` to the referenced key is what lets a label
    -- carry a COPY of its group's exclusivity that the server checks: a label
    -- claiming `group_exclusive = false` against a group whose `exclusive` is
    -- true has no row to match, so the copy cannot drift from the original.
    -- That copy is what the generated column below reads, and what the
    -- partial unique index on `issue_labels` ultimately rests on.
    --
    -- Redundant as a uniqueness CLAIM -- `id` is already the primary key, so
    -- no wider key containing it can repeat -- and not redundant as a
    -- constraint: a foreign key may reference a UNIQUE column SET and nothing
    -- else.
    CONSTRAINT label_groups_workspace_id_exclusive_key
        UNIQUE (workspace_id, id, exclusive)
);

-- Group names are unique per workspace, case-insensitively, for the reason
-- 007 gives about label names: a picker showing both "Priority" and "priority"
-- offers a choice with no meaning, and a plain UNIQUE over TEXT is
-- case-sensitive and would admit exactly that pair.
--
-- lower() rather than folding the stored value, because a group name is
-- displayed back and its capitalisation is the author's. lower() is IMMUTABLE,
-- which is what makes it legal in an index at all.
--
-- The name ends in _key rather than _idx because PostgreSQL reports the index
-- name as the constraint name in a unique-violation error, and
-- app/services/labels.py matches on it to tell a duplicate name from any other
-- write failure.
CREATE UNIQUE INDEX label_groups_workspace_name_key
    ON label_groups (workspace_id, lower(name));

-- The read order: one workspace's groups, alphabetically, with `id` as the
-- tie-break that keeps the ordering total. The same second index 007 creates
-- beside labels_workspace_name_key, for the same reason -- the listing orders
-- by the raw column, and an index over lower(name) can only answer a query
-- written in terms of lower(name).
CREATE INDEX label_groups_workspace_name_idx
    ON label_groups (workspace_id, name, id);


-- --------------------------------------------------------------------------
-- labels: which group, and the derived key the exclusivity rule is written on
-- --------------------------------------------------------------------------

-- Nullable and staying nullable: most labels belong to no group, which is a
-- real state and not a missing value. No DEFAULT, for 009's reason about
-- issues.project_id -- a default would silently file every future label under
-- whichever group it named.
ALTER TABLE labels ADD COLUMN group_id UUID;

-- The copy of `label_groups.exclusive` that labels_group_fk pins.
--
-- Denormalisation, deliberately, and it is the only kind this schema permits:
-- the value is not independent, because the composite foreign key below forces
-- it to equal the group's own column, so it cannot drift. It exists because
-- the generated column beneath it must be computable from THIS ROW ALONE --
-- PostgreSQL requires a generation expression to be immutable and same-row --
-- and "is my group exclusive" is a fact that otherwise lives one join away.
--
-- NULL exactly when `group_id` is NULL, which labels_group_exclusive_paired
-- enforces. That pairing is not tidiness; see the constraint for the hole it
-- closes.
ALTER TABLE labels ADD COLUMN group_exclusive BOOLEAN;

-- The value the exclusivity rule is actually written on, computed by the
-- server and writable by nobody.
--
-- Read it as "the thing this label competes for on an issue":
--
--   * a label in an EXCLUSIVE group competes for the group. Two such labels
--     from one group produce the same key, so a unique index over
--     (workspace, issue, key) refuses the second.
--   * every other label competes only for itself. `id` is unique across the
--     table, so no two labels ever share this key by accident, and the index
--     constrains nothing about them.
--
-- GENERATED ALWAYS ... STORED rather than a column the application writes.
-- The difference is the whole point: a plain column would be a third copy for
-- a writer to get wrong, and getting it wrong would silently disable the rule
-- for that label rather than failing. There is no INSERT and no UPDATE
-- anywhere in this system that can set this value, because PostgreSQL refuses
-- one outright.
--
-- `CASE WHEN group_exclusive THEN ... ELSE ... END` and not
-- `COALESCE(CASE ...)`: `group_exclusive` is NULL for an ungrouped label, a
-- NULL condition takes the ELSE branch, and the ELSE branch is `id`. So the
-- ungrouped case and the non-exclusive case reach the same answer by the same
-- route, which is why there is no third branch.
--
-- It cannot reference another generated column, which is why `group_exclusive`
-- above is an ordinary column pinned by a foreign key rather than itself
-- generated from the group.
--
-- This statement rewrites `labels` and takes an ACCESS EXCLUSIVE lock while it
-- does, unlike the two nullable adds above it. `labels` holds a handful of
-- rows per workspace, so the rewrite is measured in milliseconds; that is a
-- fact about this table and not a general licence.
ALTER TABLE labels ADD COLUMN exclusivity_key UUID
    GENERATED ALWAYS AS (CASE WHEN group_exclusive THEN group_id ELSE id END) STORED;

-- Both group columns, or neither.
--
-- This closes the hole MATCH SIMPLE leaves in labels_group_fk, and it is the
-- same closing 009 performs with issues_milestone_requires_project. A
-- composite foreign key is MATCH SIMPLE by default and skips its check
-- entirely for a row with ANY NULL among its referencing columns. So without
-- this a label could carry a real `group_id` and a NULL `group_exclusive`, and
-- the foreign key would never look at it -- which would not merely be untidy:
-- `exclusivity_key` would take its ELSE branch and evaluate to `id`, so the
-- label would sit in an exclusive group with its exclusivity silently switched
-- off. A rule that can be disabled by omitting a column is not a rule.
--
-- Written as an equality between two IS NULL tests rather than as two implies:
-- it says the pair moves together, in one expression, in both directions.
ALTER TABLE labels ADD CONSTRAINT labels_group_exclusive_paired
    CHECK ((group_id IS NULL) = (group_exclusive IS NULL));

-- The label's group must be a group of the label's own workspace, and the
-- label's copy of `exclusive` must be the group's.
--
-- Three columns, and each one is load-bearing. `workspace_id` is the tenancy
-- pin every composite key in this schema carries -- one column feeding both
-- this constraint and labels_workspace_fk, so a group from another tenant has
-- no row to match. `group_id` is the membership. `group_exclusive` is the
-- copy, and including it in the key is what makes the copy checked rather than
-- merely written.
--
-- ON UPDATE CASCADE, which is a deliberate departure from this schema's
-- RESTRICT-everywhere posture and needs its argument made in full.
--
-- The referenced key contains `exclusive`, so making a group exclusive -- an
-- ordinary product action, "only one status label at a time" -- CHANGES the
-- referenced key. Under RESTRICT that UPDATE is simply refused for any group
-- that has labels in it, which is every group anyone would want to change, so
-- exclusivity would be a decision that could only ever be made at creation
-- time. Under CASCADE the flip rewrites `labels.group_exclusive` for the
-- group's labels, which recomputes each one's `exclusivity_key`, which
-- cascades again into `issue_labels` -- and if any issue in the workspace is
-- already wearing two labels from the group, that second cascade violates
-- issue_labels_exclusive_group_key and the whole UPDATE fails.
--
-- That failure is the feature. "You cannot make this group exclusive while
-- issues break the rule it would impose" is exactly the right refusal, it is
-- made by the server rather than by a service that remembered to look, and it
-- is atomic with the change that provoked it.
--
-- The generic objection to CASCADE -- 009 states it at project_teams -- is
-- that a statement against one table must not silently rewrite rows in
-- another and report success. It does not apply here, and the reason is that
-- the cascaded columns are not data: `labels.group_exclusive` and
-- `issue_labels.exclusivity_key` are copies of the value being changed, whose
-- entire purpose is to equal it. Rewriting them is not a side effect of the
-- update, it IS the update, reaching the places the rule is enforced.
--
-- ON DELETE RESTRICT, unchanged. Deleting a group is not the same act as
-- changing one: the labels under it would have to go somewhere, and the two
-- defensible answers (ungroup them, delete them) are a product decision this
-- file will not make silently. LabelService removes the membership itself,
-- first, in the same transaction.
--
-- One honest note about the breadth of the CASCADE: the referenced key also
-- contains `workspace_id`, so relocating a group's tenant would rewrite its
-- labels' tenant too. Nothing can do that -- `labels_workspace_fk` and
-- `label_groups_workspace_fk` are both ON UPDATE RESTRICT against
-- `workspaces`, so a workspace's id cannot move in the first place -- but the
-- clause is wider than the argument above, and a reader is entitled to know
-- which part of it the argument covers.
ALTER TABLE labels ADD CONSTRAINT labels_group_fk
    FOREIGN KEY (workspace_id, group_id, group_exclusive)
    REFERENCES label_groups (workspace_id, id, exclusive)
    ON DELETE RESTRICT ON UPDATE CASCADE;

-- [FK TARGET] What issue_labels_exclusivity_fk references.
--
-- Redundant as a uniqueness CLAIM -- `id` is the primary key -- and not
-- redundant as a constraint: without it the join table cannot reference the
-- generated column at all, and the exclusivity rule has nothing to stand on.
--
-- Three columns and not two, for the same reason 009's
-- project_milestones_project_id_key carries three: the extra column is the one
-- being carried down, so a row on the referencing side cannot claim a
-- different value for it than the row it points at holds.
ALTER TABLE labels ADD CONSTRAINT labels_workspace_id_exclusivity_key
    UNIQUE (workspace_id, id, exclusivity_key);

-- The referencing side of labels_group_fk, and the answer to "which labels are
-- in this group".
--
-- PostgreSQL indexes the REFERENCED side of a foreign key and never the
-- referencing side, so without this every delete of -- or exclusivity flip on
-- -- a label group scans `labels` in full. The column order is the foreign
-- key's own, because that is the lookup the referential action performs.
--
-- Not partial on `group_id IS NOT NULL`, though most labels have no group, for
-- the reason 009 gives at projects_workspace_lead_idx: the predicate a
-- referential-integrity check issues is generated by PostgreSQL rather than
-- written here, so whether it matches a partial index is a property of the
-- planner's implication prover rather than of this schema.
CREATE INDEX labels_workspace_group_idx
    ON labels (workspace_id, group_id, group_exclusive);


-- --------------------------------------------------------------------------
-- issue_labels: where the exclusivity rule is finally written
-- --------------------------------------------------------------------------

-- The label's `exclusivity_key`, carried onto the association.
--
-- Added nullable, backfilled, then constrained, which is the order 005
-- establishes and rule 4 of tests/test_migration_lint.py enforces: ADD COLUMN
-- ... NOT NULL without a DEFAULT is validated against every existing row as it
-- runs, and no backfill can precede it because the column does not exist until
-- that statement finishes.
ALTER TABLE issue_labels ADD COLUMN exclusivity_key UUID;

-- Every existing association takes its label's key.
--
-- This cannot fail and cannot leave a NULL behind. No label has a group at
-- this point in the file -- `labels.group_id` was created nullable three
-- statements ago and nothing has written it -- so every `exclusivity_key` on
-- `labels` is that label's own `id`, and every `issue_labels` row has exactly
-- one label to read it from through issue_labels_label_fk.
--
-- The join carries `workspace_id` even though `label_id` alone would find the
-- row, and for the reason IssueLabelRepository.list_for_issues gives about the
-- same join: a statement that depends on a constraint holding, without saying
-- so, is a statement that silently starts crossing tenants if the constraint
-- is ever relaxed.
UPDATE issue_labels
SET exclusivity_key = labels.exclusivity_key
FROM labels
WHERE labels.workspace_id = issue_labels.workspace_id
    AND labels.id = issue_labels.label_id;

-- NOT NULL, and this one is not a matter of taste -- it is what makes the
-- foreign key below a real check rather than a decorative one.
--
-- MATCH SIMPLE skips a composite foreign key entirely for a row with any NULL
-- among its referencing columns. `workspace_id` and `label_id` are already NOT
-- NULL, so a nullable `exclusivity_key` would be the one column able to switch
-- issue_labels_exclusivity_fk off for a row -- and a row that switched it off
-- would be an association carrying no exclusivity at all, which is precisely
-- the state an attacker (or a careless INSERT) would want. With every
-- referencing column NOT NULL there is no such row: MATCH SIMPLE and MATCH
-- FULL agree, and the key is checked for every association in the table.
--
-- The cost is real and is stated so nobody meets it by surprise: an INSERT
-- into `issue_labels` must now supply this value, and the only correct source
-- for it is the label's own row. Every write in the application does it in one
-- statement -- `INSERT ... SELECT labels.exclusivity_key FROM labels` -- so
-- there is no window in which the value could be read and then invalidated.
ALTER TABLE issue_labels ALTER COLUMN exclusivity_key SET NOT NULL;

-- The carried value must be the label's own.
--
-- Without this the column would be an unchecked claim, and a writer that
-- passed `gen_random_uuid()` would defeat the rule for that row while every
-- other constraint in the database reported success.
--
-- A second foreign key from this table to `labels`, beside 007's
-- issue_labels_label_fk, and not a replacement for it. The two fire on
-- different column sets: 007's references `labels (workspace_id, id)`, so an
-- update of `labels.exclusivity_key` does not touch its referenced key and its
-- ON UPDATE RESTRICT does not run; this one references the key that DOES
-- change and cascades. Leaving 007's in place also keeps this file free of any
-- change to an applied migration.
--
-- ON DELETE CASCADE, matching both of 007's keys on this table and resting on
-- the same argument: an `issue_labels` row carries no data of its own, and
-- deleting a label is exactly how a workspace stops using one.
--
-- ON UPDATE CASCADE, for the reason labels_group_fk gives at length -- this is
-- the second link in that chain, and the link that lands the change on the
-- index that enforces the rule.
ALTER TABLE issue_labels ADD CONSTRAINT issue_labels_exclusivity_fk
    FOREIGN KEY (workspace_id, label_id, exclusivity_key)
    REFERENCES labels (workspace_id, id, exclusivity_key)
    ON DELETE CASCADE ON UPDATE CASCADE;

-- THE RULE: at most one label from an exclusive group, per issue.
--
-- A partial unique index, and both halves of it are exact.
--
-- The KEY is (workspace_id, issue_id, exclusivity_key). Two labels from one
-- exclusive group produce the same third column on the same issue, so the
-- second association is refused by the server -- not by a service that
-- remembered to count first, and not with a window between the count and the
-- insert for a concurrent attach to slip through.
--
-- The PREDICATE is `exclusivity_key <> label_id`, which reads as an odd thing
-- to write and is the precise statement of "this label is in an exclusive
-- group". `labels.exclusivity_key` is the group's id for exactly those labels
-- and the label's own id for every other, so the inequality is a same-row test
-- for the property, computable without a join, and IMMUTABLE -- which is what
-- makes it legal in an index predicate at all.
--
-- Written partial rather than as a plain UNIQUE over the same three columns,
-- and the difference matters for a reason that has nothing to do with size. A
-- non-partial index would ALSO be violated by attaching the same ungrouped
-- label twice -- its key would be (workspace, issue, that label's id), which
-- is issue_labels_pkey spelled differently -- so PostgreSQL would raise
-- whichever of the two constraints it happened to check first, and
-- LabelService would report "already applied" or "excluded by group" more or
-- less at random. The predicate removes every such row from this index, so a
-- duplicate attach can only ever be issue_labels_pkey and a group conflict can
-- only ever be this.
--
-- Named `_key` rather than `_idx` because PostgreSQL reports an index name as
-- the constraint name in a unique-violation error, and app/services/labels.py
-- matches on it. 007's labels_workspace_name_key is named for the same reason.
CREATE UNIQUE INDEX issue_labels_exclusive_group_key
    ON issue_labels (workspace_id, issue_id, exclusivity_key)
    WHERE exclusivity_key <> label_id;

-- No index is created for the referencing side of issue_labels_exclusivity_fk,
-- and that is a decision rather than an omission. The lookup a referential
-- action performs here is on (workspace_id, label_id, exclusivity_key), and
-- 007's issue_labels_workspace_label_idx is (workspace_id, label_id) -- the
-- leading prefix of exactly that. PostgreSQL scans it for the first two
-- columns and rechecks the third on the handful of rows a single label
-- contributes, which is the same work a three-column index would do without a
-- second copy of the same data on every insert.
