-- GitHub automations: which repositories a workspace tracks, which state a
-- pull request moves an issue to, and how the history says a machine did it.
--
-- 017 stored the LINK between a pull request and an issue and stopped there.
-- Nothing in the product has ever moved an issue in response to a pull
-- request, and this file is the schema half of doing so. It adds no new
-- concept of its own: the move is an ordinary `issues.workflow_state_id`
-- write, the audit row is an ordinary `issue_activity` row, and the only
-- genuinely new thing is a per-team statement of WHICH state to move to.
--
-- Three changes, and they are one feature rather than three:
--
--   * `github_repositories.tracked` -- an installation covering two hundred
--     repositories is not a workspace that wants development activity from
--     two hundred repositories.
--   * `issue_activity.caused_by` -- an issue that moved with no actor and no
--     reason is worse than an issue that did not move.
--   * `github_issue_automations` -- the per-team configuration, which is the
--     part that cannot be derived.
--
-- Why the configuration cannot be derived
-- ---------------------------------------
-- 005 makes workflow states TEAM-scoped and USER-named. There is no global
-- "In Progress": a team may call it "Building", may have three states of
-- category `started` (Building, In Review, Blocked), and may reorder or
-- rename any of them tomorrow. `type` is the only portable concept, and it
-- does not narrow to one row.
--
-- So this table stores the answer rather than computing it. A workspace that
-- has not answered gets no automation at all -- the absence of a row is the
-- off switch, and it is also the refusal to guess. The DEFAULT derived from
-- category (the lowest-`position` state of the right type) is applied ONCE,
-- by the service, at the moment an admin turns the automation on, and stored
-- explicitly. Nothing at delivery time ever picks a state on a workspace's
-- behalf: by then the row either names one or the trigger does nothing.
--
-- Apply this ONLY through `python -m scripts.apply_migration
-- migrations/030_github_automations.sql`. The runner wraps the file in one
-- transaction, takes an advisory lock and records a checksum; a hand-run gets
-- none of that and executes statement-at-a-time in autocommit, which here
-- would leave `github_repositories` carrying a column the application reads
-- as a filter while the table it filters for does not exist.
--
-- DEPENDS ON:
--
--   * 005, for `workflow_states` and `workflow_states_team_id_key` -- the
--     UNIQUE (workspace_id, team_id, id) both state references below target.
--   * 002, for `teams_workspace_id_key`.
--   * 012, for `issue_activity`.
--   * 013, for `github_repositories`.
--
-- Applying this before any of them fails on the constraint that needs it and,
-- inside the runner's single transaction, leaves nothing behind. That failure
-- IS the dependency check: the ledger records what has been applied and not
-- what depends on what.


-- --------------------------------------------------------------------------
-- Which repositories this workspace actually wants
-- --------------------------------------------------------------------------

-- Whether deliveries about this repository are applied.
--
-- A column on the existing row rather than a second table listing the chosen
-- ones, because the question is a property of a repository the installation
-- already covers and every reader already has that row in hand:
-- `repository_exists` -- the one gate both `pull_request` and `push` pass
-- through -- becomes one extra predicate rather than a join.
--
-- NOT NULL DEFAULT TRUE, and the default is the compatibility story rather
-- than a preference. Every repository already recorded, and every repository
-- a future `installation` delivery seeds, is tracked; a workspace that never
-- opens the setting behaves exactly as it did before this file. Opting OUT is
-- the act that takes a decision, which is the right way round for a column
-- that decides whether a webhook is applied.
--
-- It also makes the ADD COLUMN metadata-only on PostgreSQL 11 and later --
-- existing rows are not rewritten -- which is the difference between this
-- statement and one that would take an exclusive lock on the table for as
-- long as it takes to rewrite every row.
--
-- Untracking does NOT delete the development history already collected. That
-- history was gathered while the repository was tracked and is what an issue's
-- Development section is showing; discarding it would rewrite the past to
-- match a decision taken today. What untracking stops is new deliveries.
ALTER TABLE github_repositories
    ADD COLUMN tracked BOOLEAN NOT NULL DEFAULT TRUE;

-- Deliberately NO index on `tracked`.
--
-- The only read that filters on it is `repository_exists`, which is already an
-- equality on `github_repositories_pkey` -- (workspace_id, repository_id) --
-- and returns at most one row. `tracked` is a filter applied to that one row,
-- not a way of finding it. A partial index would be a write cost with no
-- reader, which is the argument 017 makes about the repository-scoped commit
-- listing nobody performs.


-- --------------------------------------------------------------------------
-- Why a row in the history exists
-- --------------------------------------------------------------------------

-- What caused this activity row, when it was not a person.
--
-- 012 says `actor_id IS NULL` means "a system action -- an import, a scheduled
-- transition, a future automation" and leaves it there, which was honest while
-- no automation existed. It does now, and "nobody moved this issue to Done"
-- is a worse answer than the issue never having moved: a reader looking at the
-- timeline has to be able to see that a pull request did it, and which one.
--
-- A column beside `actor_id` rather than a thirteenth `kind`. The kind is what
-- MOVED -- the state changed, and that is `state_changed` whoever did it -- so
-- a parallel `state_changed_by_pull_request` would fork every renderer in
-- every client for a fact that is not about the change. This is orthogonal to
-- `kind` and reads as one more column of the same sentence.
--
-- Nullable, and NULL is the overwhelming majority: a row with an `actor_id` is
-- caused by that person and needs nothing further. NOT the same column as
-- `github_pull_request_issues.source`, which answers "where was the identifier
-- written" -- hence `caused_by` rather than a second `source` in one schema.
--
-- TEXT and opaque to the database, exactly as `from_value` and `to_value` are
-- and for the reason 012 gives about them: this table answers one query, "this
-- issue's history, newest first", and nothing joins on, orders by or compares
-- this column. The format the application writes is
-- `github_pull_request:<owner>/<name>#<number>`; a reader that does not
-- recognise a prefix renders the row exactly as it renders one with no cause
-- at all, which is what makes a fourteenth cause a code change in one client
-- rather than a migration.
ALTER TABLE issue_activity ADD COLUMN caused_by TEXT;

-- A bound, not a validation. The value is written by this application from
-- values GitHub signed, never by a client, so its content is not a trust
-- question -- but an unbounded TEXT column on an append-only table is a way to
-- make it enormous, and 200 is well past the longest `owner/name#number` a
-- GitHub repository can produce. Empty is refused because an empty cause is
-- the NULL this column already has a spelling for.
ALTER TABLE issue_activity ADD CONSTRAINT issue_activity_caused_by_length
    CHECK (caused_by IS NULL OR length(caused_by) BETWEEN 1 AND 200);


-- --------------------------------------------------------------------------
-- Which state a pull request moves an issue to
-- --------------------------------------------------------------------------

-- One team's answer to "what does a pull request do to an issue".
--
-- Keyed on the TEAM and not the workspace, because that is where workflow
-- states live (005) and a workspace with an engineering team and a design team
-- has two different boards. A workspace-level setting would have to name a
-- state belonging to one team and then either apply it to issues on another --
-- which `issues_workflow_state_fk` refuses outright -- or silently do nothing
-- for every team but one.
--
-- The absence of a row is the off switch. There is no `enabled BOOLEAN`:
-- disabled and unconfigured are the same state, an admin who has never opened
-- the setting is in it, and a boolean beside two nullable ids would make
-- `enabled = true` with both ids NULL a third state meaning exactly what
-- `enabled = false` means. Deleting the row is how it is turned off.
CREATE TABLE github_issue_automations (
    -- Both halves of the tenant pair, exactly as workflow_states carries them,
    -- and for the same reason: it is what lets the two state references below
    -- be composite over (workspace_id, team_id, id), so that "this state
    -- belongs to this team in this workspace" is one tuple the server checks
    -- rather than three facts application code has to line up.
    workspace_id UUID NOT NULL,
    team_id UUID NOT NULL,

    -- Where an issue goes when a pull request naming it OPENS -- or reopens,
    -- or leaves draft. NULL means that trigger does nothing for this team,
    -- which is a real choice: a team may want the merge automated and the
    -- start left to whoever is doing the work.
    started_state_id UUID,

    -- Where an issue goes when a pull request naming it MERGES.
    --
    -- Merges, and not closes. GitHub reports a merged pull request as
    -- `state = 'closed'` with `merged_at` set, and 017 stores the three source
    -- fields separately precisely so the difference survives: a pull request
    -- closed without merging is an abandoned attempt and moving the issue to a
    -- completed state for one would report work as finished that nobody did.
    completed_state_id UUID,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- One row per team. Also the index every read here uses: the delivery path
    -- joins this table by (workspace_id, team_id) for the teams of the issues
    -- a pull request links to, and the settings screen reads one workspace's
    -- rows by its leading column.
    CONSTRAINT github_issue_automations_pkey PRIMARY KEY (workspace_id, team_id),

    CONSTRAINT github_issue_automations_team_fk
        FOREIGN KEY (workspace_id, team_id)
        REFERENCES teams (workspace_id, id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    -- The configured state must be one of THIS team's states, in THIS
    -- workspace. A single-column reference to workflow_states.id would accept
    -- a row pointing a team's automation at another team's board -- and then
    -- the move itself would fail on `issues_workflow_state_fk` at delivery
    -- time, inside a webhook transaction, as a 500 GitHub redelivers forever.
    --
    -- MATCH SIMPLE, which is the default and is load-bearing here rather than
    -- incidental: a composite foreign key skips its check entirely when ANY
    -- referencing column is NULL, so a NULL `started_state_id` is not checked
    -- against anything -- which is exactly what "this trigger does nothing"
    -- has to mean. 005 makes the opposite argument about
    -- `issues.workflow_state_id` and both are right: there, NULL would switch
    -- off a check that must always run, so the column is NOT NULL; here, NULL
    -- IS the absence of a configured state.
    --
    -- RESTRICT on both sides, in line with 005: deleting a workflow state that
    -- an automation names must fail loudly and be resolved by reconfiguring
    -- the automation, not by silently pointing it at nothing.
    CONSTRAINT github_issue_automations_started_state_fk
        FOREIGN KEY (workspace_id, team_id, started_state_id)
        REFERENCES workflow_states (workspace_id, team_id, id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    CONSTRAINT github_issue_automations_completed_state_fk
        FOREIGN KEY (workspace_id, team_id, completed_state_id)
        REFERENCES workflow_states (workspace_id, team_id, id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    -- A row that moves nothing is a row that means what no row means. Without
    -- this the table grows entries an admin cannot tell from an absence, and
    -- the settings screen has to render "automation on, doing nothing".
    CONSTRAINT github_issue_automations_moves_something
        CHECK (started_state_id IS NOT NULL OR completed_state_id IS NOT NULL)
);

-- The CATEGORY of each configured state is deliberately NOT constrained here.
--
-- It is a real rule -- the started slot must name a `started` state and the
-- completed slot a `completed` one -- and it is enforced in the service, where
-- breaking it is a named field error an admin can correct. A CHECK cannot
-- express it (it would have to join `workflow_states`), and the two remaining
-- options are a trigger, which this project has deliberately not taken up, and
-- a denormalised copy of `type` in this table, which is a second copy of a
-- value a team can change and therefore a way for the two to disagree.

-- No index beyond the primary key.
--
-- Both state foreign keys are RESTRICT, so `DELETE FROM workflow_states`
-- performs a referential lookup here -- but this table holds at most one row
-- per team, and the primary key's leading (workspace_id, team_id) is the
-- prefix that lookup probes. The referencing side is therefore already served;
-- an index on `started_state_id` alone would buy nothing that a one-row-per-
-- team scan does not already give.
