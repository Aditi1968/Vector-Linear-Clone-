-- Who is watching an issue, and the prefilled shapes a new issue can be
-- filed from.
--
-- Two features in one file because they are one migration's worth of schema
-- and both hang off the same pair of parents -- `issues` and
-- `workspace_members` -- through the same composite keys. They do not
-- otherwise interact, and neither reads the other.
--
-- SUBSCRIBERS is the answer to "who else should hear about this". Migration
-- 012 built the inbox and derived its recipients from the issue's own row:
-- the assignee, plus the creator for the kinds an author cares about. That
-- set cannot express the person who commented once and wants to follow the
-- thread, or the lead who is watching a launch issue they neither filed nor
-- own -- and there is no column on `issues` such a person could be recorded
-- in, because there are many of them. `issue_subscribers` is that column made
-- into a table, and every `notifications` row written from here on is fanned
-- out through it.
--
-- TEMPLATES is stored, attacker-controlled data that names other rows. That
-- is the whole reason this half needs care rather than a JSONB blob: a
-- template holds an assignee, a project, a cycle and a set of labels, and
-- every one of those is an id somebody typed into a form. A template is
-- written once and applied many times, possibly months later, so a mis-scoped
-- id here is not one bad request -- it is a bad request replayed by every
-- person who uses the template. Every reference below is therefore composite
-- through `workspace_id`, exactly as 009 does for `projects.lead_id`: a
-- template in workspace A pointing at workspace B's project is not a row
-- PostgreSQL will store, so the service applying it cannot be tricked by data
-- it read back from its own database.
--
-- Apply this ONLY through `python -m scripts.apply_migration
-- migrations/020_subscribers_templates.sql`. The runner wraps the file in one
-- transaction, takes an advisory lock and records a checksum; a hand-run gets
-- none of that and executes statement-at-a-time in autocommit, which here
-- means the notifications CHECK below could be dropped and never replaced --
-- leaving the inbox's vocabulary open for as long as it took somebody to
-- notice.
--
-- DEPENDS ON:
--
--   * 002, for `workspaces` and `teams_workspace_id_key`.
--   * 004, for `workspace_members`, whose primary key both the subscriber
--     table and the template assignee reference.
--   * 007, for `issues_workspace_id_key` and for `labels_workspace_id_key`.
--   * 008, for `cycles_workspace_team_id_key`.
--   * 009, for `projects_workspace_id_key`.
--   * 012, for `notifications` and the CHECK this file widens. Widening a
--     constraint that does not exist yet is a hard failure, which is the
--     dependency check doing its job.
--
-- Applying this before any of them fails on the constraint that needs it and,
-- inside the runner's single transaction, leaves nothing behind. That failure
-- IS the dependency check: the ledger records what has been applied and not
-- what depends on what, so the schema itself is what has to refuse an
-- out-of-order apply.


-- One person watching one issue.
--
-- No surrogate id: the row IS its key, exactly as `project_teams` in 009 and
-- `issue_labels` in 007. An `id UUID PRIMARY KEY` here would need a
-- UNIQUE (workspace_id, issue_id, user_id) beside it to stop the same person
-- subscribing twice, so it would be a second key that buys nothing and one
-- more column for a mutation to get wrong.
--
-- No `subscribed BOOLEAN` and no tombstone for somebody who unwatched. That
-- shape is what a product needs when unwatching must SURVIVE later
-- participation, and this product's rule is the opposite one: commenting on
-- an issue subscribes you to it again, because commenting is asking to be
-- part of the conversation. Unwatch is therefore a DELETE, and the absence of
-- a row is the whole of "not watching" -- one state, spelled one way. A
-- tombstone would add a second spelling of it that every read has to filter
-- for and every writer has to remember.
CREATE TABLE issue_subscribers (
    -- One workspace_id for the whole row, and that is the point. Both foreign
    -- keys below read this same column, so the issue and the watcher are
    -- checked against the SAME tenant rather than against two tenants that
    -- happen to be spelled separately. A pair of single-column foreign keys --
    -- issue_id -> issues.id, user_id -> users.id -- would accept a row
    -- subscribing workspace B's user to workspace A's issue, and neither
    -- constraint would notice; the subscriber would then receive a
    -- notification naming an issue they have no access to, which is 012's
    -- leak reintroduced one table over.
    workspace_id UUID NOT NULL,

    issue_id UUID NOT NULL,
    user_id UUID NOT NULL,

    -- No updated_at. Nothing about a subscription can change: it exists or it
    -- does not, and re-subscribing an existing watcher is a no-op that must
    -- not move this timestamp -- "watching since" is what it means.
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- The primary key is also the uniqueness rule ("a person watches an issue
    -- once") and also the index answering "who watches this issue", since
    -- (workspace_id, issue_id) is its leading prefix -- which is the read the
    -- notification fan-out performs on every comment and every status change.
    CONSTRAINT issue_subscribers_pkey
        PRIMARY KEY (workspace_id, issue_id, user_id),

    -- ON DELETE RESTRICT, matching `issue_activity` and `notifications` in
    -- 012 rather than 007's CASCADE on comments. Nothing deletes an issue --
    -- this product archives, for the reasons 006 gives -- so the choice is
    -- between two refusals no path reaches, and RESTRICT is the one that makes
    -- whoever writes the first delete path decide out loud.
    CONSTRAINT issue_subscribers_issue_fk
        FOREIGN KEY (workspace_id, issue_id)
        REFERENCES issues (workspace_id, id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    -- The watcher must be a member of THIS issue's workspace.
    --
    -- `REFERENCES users (id)` is the obvious spelling and it is the bug, for
    -- the reason 009 gives at projects_lead_fk and 012 at
    -- notifications_user_fk: it checks that the watcher is a real account
    -- somewhere and says nothing about where. A subscription is a LIVE claim
    -- about the present -- "notify this person about this issue from now on"
    -- -- so it lands on the same side of 006's assignee/creator distinction as
    -- the inbox does, and it stops being true the moment the person leaves.
    --
    -- The reference is composite onto `workspace_members (workspace_id,
    -- user_id)`, which is that table's PRIMARY KEY, marked [FK TARGET] in 004
    -- for exactly this. One `workspace_id` column feeds both this constraint
    -- and the issue one above, so a non-member watcher is not a row PostgreSQL
    -- will store, whatever the application does or forgets.
    --
    -- MATCH SIMPLE's NULL exemption is unreachable here: every referencing
    -- column is NOT NULL, so unlike projects_lead_fk there is no
    -- "this row has no watcher" case to reason about.
    --
    -- ON DELETE RESTRICT, matching notifications_user_fk, and with the same
    -- consequence stated out loud: a member who watches an issue cannot be
    -- removed from the workspace until their subscriptions are cleared.
    -- 012 already put `notifications` in that position and said the removal
    -- path owns the choice; this adds a second table to the same decision
    -- rather than making a different one quietly. For a subscription, as for
    -- an inbox, deleting the rows is almost certainly the right answer -- and
    -- this file will not make that call on the removal path's behalf.
    CONSTRAINT issue_subscribers_user_fk
        FOREIGN KEY (workspace_id, user_id)
        REFERENCES workspace_members (workspace_id, user_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT
);

-- The reverse direction: "which issues am I watching", and, more importantly,
-- the referencing side of issue_subscribers_user_fk.
--
-- PostgreSQL indexes the REFERENCED side of a foreign key and never the
-- referencing side, so without this every deletion from `workspace_members`
-- scans `issue_subscribers` in full to satisfy the RESTRICT above -- on the
-- path of every member removal in every workspace. The forward direction
-- needs no index of its own: (workspace_id, issue_id) is the leading prefix of
-- issue_subscribers_pkey, which also serves issue_subscribers_issue_fk.
CREATE INDEX issue_subscribers_workspace_user_idx
    ON issue_subscribers (workspace_id, user_id);


-- The inbox vocabulary, widened by one.
--
-- 012 admitted three kinds and argued for the shortness: everything that
-- happens to an issue belongs in its history, almost none of it belongs in
-- anybody's inbox. A fourth is earned by subscribers rather than assumed with
-- them -- 'status_changed' is the event a watcher is watching FOR. Someone who
-- asked to follow an issue asked to know when it moves, and until now the only
-- way to learn that was to open it.
--
-- Not 'state_changed', which is what `issue_activity` calls the same event.
-- The two vocabularies are deliberately separate -- twelve kinds against four
-- -- and the inbox's names are the ones a client renders into a sentence a
-- person reads. "Status" is the word the product uses on screen.
--
-- Dropped and re-added rather than altered, which is the only way to widen a
-- CHECK, and the reason 009's projects_state_check is a CHECK rather than an
-- enum type in the first place: PostgreSQL refuses to USE an enum label added
-- by `ALTER TYPE ... ADD VALUE` in the transaction that added it, so a fifth
-- kind under an enum would have to be split across two files or run outside
-- the runner. Both statements are inside the runner's single transaction, so
-- there is no instant at which the column is unconstrained.
--
-- Widening only. Every kind 012 admitted is still admitted, so no existing row
-- can fail the new constraint and the re-add's validation scan cannot abort
-- the migration.
ALTER TABLE notifications DROP CONSTRAINT notifications_kind_known;

ALTER TABLE notifications ADD CONSTRAINT notifications_kind_known
    CHECK (kind IN ('assigned', 'commented', 'blocked', 'status_changed'));


-- A prefilled shape a new issue can be filed from.
--
-- Every column below except `name` is a DEFAULT for the issue, not a fact
-- about the template: `title` is the title a new issue starts with, and `name`
-- is what the template is called in the menu. They are different strings and
-- the distinction is easy to lose -- a template called "Bug report" whose
-- issues are all titled "Bug report" is what happens when one column tries to
-- be both.
--
-- Explicit typed columns rather than a JSONB `defaults` payload, and the
-- argument is stronger here than it was for `issue_activity` in 012. A JSONB
-- blob cannot carry a foreign key, so the assignee, project, cycle and labels
-- inside one would be unvalidated text -- and this is precisely the data that
-- must not be trusted, because it is stored, replayed, and written by whoever
-- can edit a template. Typed columns are what let the composite keys below
-- exist at all, which is what makes "apply this template" unable to reach
-- another tenant even if the code applying it is wrong.
CREATE TABLE issue_templates (
    id UUID PRIMARY KEY DEFAULT uuidv7(),

    workspace_id UUID NOT NULL,

    -- Which team's menu this template appears in, or NULL for the whole
    -- workspace's.
    --
    -- Nullable, and the NULL is a real state rather than a missing value:
    -- "Bug report" is a shape every team files, and forcing a copy per team
    -- would mean editing six rows to fix one typo. A team-scoped template is
    -- the other real case -- an on-call runbook that only means something to
    -- infrastructure -- so both exist and one column expresses both.
    --
    -- Deliberately not a `scope TEXT CHECK (scope IN ('workspace','team'))`
    -- beside a nullable team_id: two columns encoding one fact are two columns
    -- that can disagree, and the disagreement surfaces as a template that
    -- claims to be workspace-wide while carrying a team id the cycle key below
    -- still validates against.
    team_id UUID,

    -- What the template is called. Not unique: two teams may each want a
    -- "Bug report", and a uniqueness rule spanning the NULL team would refuse
    -- the second one for a reason the person filing it cannot see.
    name TEXT NOT NULL,

    -- The issue defaults. Every one nullable, because a template that fills in
    -- two fields and leaves the rest to the person filing is the ordinary
    -- case; NULL here means "this template has no opinion", never "empty".
    -- That distinction is why `title` is nullable rather than defaulted to ''
    -- -- an empty title is one `issues_title` would have to accept, and a
    -- template with no title should leave the field for the author.
    title TEXT,
    description TEXT,
    priority INTEGER,
    estimate INTEGER,

    -- The three references. Each is checked against this row's single
    -- workspace_id below; see the constraints for what each one refuses.
    assignee_id UUID,
    project_id UUID,
    cycle_id UUID,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT issue_templates_workspace_fk
        FOREIGN KEY (workspace_id)
        REFERENCES workspaces (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    -- The team must be in this template's workspace. MATCH SIMPLE skips the
    -- check for a NULL team_id, which is exactly the wanted behaviour and is
    -- only safe because `workspace_id` is NOT NULL: the sole column that can
    -- be NULL is team_id, so the exemption is precisely "this template belongs
    -- to the whole workspace" and cannot be widened by a NULL arriving in the
    -- other half.
    CONSTRAINT issue_templates_team_fk
        FOREIGN KEY (workspace_id, team_id)
        REFERENCES teams (workspace_id, id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    -- The default assignee must be a member of THIS template's workspace,
    -- for the reason 009 gives at projects_lead_fk. The attack this refuses is
    -- specific: a template naming a user id from another tenant would, on
    -- every apply, try to file an issue assigned to somebody outside the
    -- workspace -- and `issues_assignee_fk` would then refuse the apply, so
    -- the damage would be a template that silently never works rather than a
    -- leak. Refusing it at SAVE time is what turns that into an error the
    -- person editing the template can see and correct.
    CONSTRAINT issue_templates_assignee_fk
        FOREIGN KEY (workspace_id, assignee_id)
        REFERENCES workspace_members (workspace_id, user_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    CONSTRAINT issue_templates_project_fk
        FOREIGN KEY (workspace_id, project_id)
        REFERENCES projects (workspace_id, id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    -- The cycle must belong to the template's own TEAM -- not merely to the
    -- same workspace. Three columns are what buys that, exactly as
    -- issues_cycle_fk in 008 does for the issue itself: a two-column key onto
    -- some (workspace_id, id) would happily accept another team's cycle, and
    -- every issue filed from the template would then be refused by
    -- issues_cycle_fk at apply time for a reason nothing here had reported.
    CONSTRAINT issue_templates_cycle_fk
        FOREIGN KEY (workspace_id, team_id, cycle_id)
        REFERENCES cycles (workspace_id, team_id, id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    -- The hole MATCH SIMPLE leaves, closed -- 009's
    -- issues_milestone_requires_project, applied to the same shape one table
    -- over. issue_templates_cycle_fk is skipped entirely for any row with a
    -- NULL among its three referencing columns, and `team_id` is one of them.
    -- So without this check a row holding `team_id IS NULL` and a real
    -- `cycle_id` passes the foreign key untested: a workspace-wide template
    -- pointing at one team's cycle, which no issue filed from it could ever
    -- satisfy.
    CONSTRAINT issue_templates_cycle_requires_team
        CHECK (cycle_id IS NULL OR team_id IS NOT NULL),

    -- Bounds, not validation. These columns are echoed back into an issue and
    -- rendered, and every one of them is written by whoever may edit a
    -- template; an unbounded TEXT column is a way to make a row -- and the
    -- response carrying it -- arbitrarily large. The numbers match what the
    -- issue itself accepts, so a template cannot be saved holding a value
    -- `IssueService` would refuse: `issues_title` is capped at 500 characters
    -- by TITLE_MAX_LENGTH, `comments_body_length` in 007 caps prose at 16384,
    -- `issues_priority_range` in 001 admits 0..4, and
    -- `issues_estimate_non_negative` in 006 sets no ceiling.
    CONSTRAINT issue_templates_name_length
        CHECK (length(name) BETWEEN 1 AND 100),

    CONSTRAINT issue_templates_title_length
        CHECK (title IS NULL OR length(title) BETWEEN 1 AND 500),

    CONSTRAINT issue_templates_description_length
        CHECK (description IS NULL OR length(description) BETWEEN 1 AND 16384),

    CONSTRAINT issue_templates_priority_range
        CHECK (priority IS NULL OR priority BETWEEN 0 AND 4),

    CONSTRAINT issue_templates_estimate_non_negative
        CHECK (estimate IS NULL OR estimate >= 0),

    -- [FK TARGET] What issue_template_labels references. Redundant beside the
    -- primary key on id only if you do not look at what points here: a foreign
    -- key may reference a UNIQUE column SET and nothing else, so without this
    -- the join below falls back to a single-column key onto `id` -- which
    -- permits exactly the cross-workspace row this file exists to refuse.
    CONSTRAINT issue_templates_workspace_id_key UNIQUE (workspace_id, id)
);


-- The labels a template applies.
--
-- A join table rather than a `label_ids UUID[]` column, and the difference is
-- the whole point of this file: an array cannot carry a foreign key, so every
-- id in it would be unvalidated text that the apply path had to check itself
-- -- against a workspace it read from the same untrusted row. One row per
-- label is what lets labels_workspace_id_key refuse another tenant's label at
-- the moment the template is saved.
--
-- No surrogate id, for the reason `issue_labels` in 007 gives: the row is its
-- key.
CREATE TABLE issue_template_labels (
    -- One workspace_id, read by BOTH foreign keys below, so the template and
    -- the label are checked against the SAME tenant. A pair of single-column
    -- keys would accept workspace A's template carrying workspace B's label,
    -- and the apply path would then hand `issue_labels` a label id from
    -- outside the workspace -- refused there, but only because that table made
    -- the same choice this one is making.
    workspace_id UUID NOT NULL,

    template_id UUID NOT NULL,
    label_id UUID NOT NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Also the uniqueness rule ("a template carries a label once") and also
    -- the index answering "which labels does this template apply", since
    -- (workspace_id, template_id) is its leading prefix.
    CONSTRAINT issue_template_labels_pkey
        PRIMARY KEY (workspace_id, template_id, label_id),

    -- RESTRICT rather than CASCADE, in both directions, for the reason 009
    -- gives about project_teams: a single `DELETE FROM labels` would quietly
    -- strip that label from every template in the workspace while the command
    -- tag read `DELETE 1`. TemplateService removes these rows itself, in the
    -- same transaction, before deleting the template -- so the constraint is a
    -- guard on that ordering rather than an obstacle to it.
    CONSTRAINT issue_template_labels_template_fk
        FOREIGN KEY (workspace_id, template_id)
        REFERENCES issue_templates (workspace_id, id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    CONSTRAINT issue_template_labels_label_fk
        FOREIGN KEY (workspace_id, label_id)
        REFERENCES labels (workspace_id, id)
        ON DELETE RESTRICT ON UPDATE RESTRICT
);


-- The one read the template list serves: this workspace's templates in the
-- order a menu shows them.
--
-- `id` is the tie-break that makes the order total when two templates share a
-- name, which is allowed here on purpose. It is in the index so the ordering
-- is read rather than sorted.
--
-- No `team_id` in this index, though every listing filters on it. The filter
-- is `team_id IS NULL OR team_id = $2` -- a workspace's templates plus one
-- team's -- which is an OR the planner cannot turn into an index bound
-- whichever way the columns are ordered. Templates are a configuration list
-- of tens, not a product list of millions, so the recheck is cheap and this
-- index is the one that matters.
CREATE INDEX issue_templates_workspace_name_idx
    ON issue_templates (workspace_id, name, id);

-- One index for two foreign keys: (workspace_id, team_id) is the prefix
-- issue_templates_team_fk's RESTRICT check looks up, and all three columns are
-- what issue_templates_cycle_fk's does. Without it, deleting a team or a cycle
-- scans every template in the database. The same shape, and the same
-- reasoning, as issues_workspace_project_milestone_idx in 009 -- including its
-- refusal to be partial on `team_id IS NOT NULL`: the predicate a referential
-- check issues is generated by PostgreSQL rather than written here, so whether
-- it matches a partial index is a property of the planner's implication prover
-- rather than of this schema.
CREATE INDEX issue_templates_workspace_team_cycle_idx
    ON issue_templates (workspace_id, team_id, cycle_id);

-- The referencing side of issue_templates_assignee_fk, and the reason it is
-- here rather than deferred: without it every removal from
-- `workspace_members` scans `issue_templates` in full -- the same cost 009's
-- projects_workspace_lead_idx exists to avoid, on the same path.
CREATE INDEX issue_templates_workspace_assignee_idx
    ON issue_templates (workspace_id, assignee_id);

-- The referencing side of issue_templates_project_fk. A project deletion is
-- rarer than a member removal, and it already scans `issues` without this
-- index existing for that table either -- 009 gave `issues` one for exactly
-- this reason, and a template is one more table that would otherwise be
-- scanned.
CREATE INDEX issue_templates_workspace_project_idx
    ON issue_templates (workspace_id, project_id);

-- The reverse direction of the label join: "which templates use this label",
-- and the referencing side of issue_template_labels_label_fk. Without it every
-- `DELETE FROM labels` scans the join in full to satisfy its RESTRICT. The
-- forward direction needs no index of its own: (workspace_id, template_id) is
-- the leading prefix of issue_template_labels_pkey.
CREATE INDEX issue_template_labels_workspace_label_idx
    ON issue_template_labels (workspace_id, label_id);
