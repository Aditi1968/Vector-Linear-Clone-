-- Projects: a workspace-owned unit of work that many teams collaborate on,
-- the milestones inside it, and the issues that belong to both.
--
-- The shape this file exists to make impossible is `projects.team_id`. A
-- project with a single owning team reads naturally and is wrong for the
-- product: a launch is engineering plus design plus docs, and modelling that
-- as one team forces every other team's work out of the project or forces a
-- duplicate project per team. Ownership is therefore a workspace, and team
-- participation is a row in `project_teams`.
--
-- The second thing it makes impossible is a cross-tenant association. Every
-- table below carries `workspace_id` and every foreign key out of it is
-- composite, following the pattern migrations/002_tenancy.sql establishes with
-- `issues (workspace_id, team_id) -> teams (workspace_id, id)`. A join row
-- holds ONE workspace_id and points at both parents through it, so "project in
-- workspace A, team from workspace B" is not a row PostgreSQL will store --
-- there is no column left for the second workspace to go in. That is the whole
-- reason the join table is keyed on three columns rather than two.
--
-- Apply this ONLY through `python -m scripts.apply_migration
-- migrations/009_projects.sql`. The runner wraps the file in one transaction,
-- takes an advisory lock and records a checksum; a hand-run gets none of that
-- and executes statement-at-a-time in autocommit, which for this file means a
-- failure part way through leaves `issues` carrying two nullable columns with
-- no constraints behind them.


CREATE TABLE projects (
    id UUID PRIMARY KEY DEFAULT uuidv7(),

    workspace_id UUID NOT NULL,

    name TEXT NOT NULL,
    description TEXT,

    -- A TEXT column with a CHECK, deliberately not an enum type.
    --
    -- The runner wraps each migration file in a single transaction, and
    -- PostgreSQL refuses to *use* an enum label added by `ALTER TYPE ... ADD
    -- VALUE` in the same transaction that added it. So the migration that one
    -- day adds a sixth state would have to be split across two files, or run
    -- outside the runner, or carry a `pg_enum` insert written by hand. A CHECK
    -- widens with `DROP CONSTRAINT` + `ADD CONSTRAINT` in one file, which
    -- keeps a state change looking like every other schema change here.
    --
    -- What the CHECK does not buy is ordering or transitions: it says which
    -- states exist, not which may follow which. That rule is the service's,
    -- because it depends on who is asking, and a database has no way to know.
    state TEXT NOT NULL,

    -- DATE, not TIMESTAMPTZ. A target date is a day on a calendar that people
    -- in several timezones agree on; storing an instant makes "due the 30th"
    -- answer differently either side of midnight UTC, and there is no instant
    -- a user ever meant to pick.
    target_date DATE,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- RESTRICT on both sides, matching teams_workspace_fk. A workspace with
    -- live projects is not something to delete by accident, and relocating a
    -- workspace's id is not something to do silently to its projects.
    CONSTRAINT projects_workspace_fk
        FOREIGN KEY (workspace_id)
        REFERENCES workspaces (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    CONSTRAINT projects_state_check
        CHECK (state IN ('planned', 'started', 'paused', 'completed', 'canceled')),

    -- [FK TARGET] Redundant beside the primary key on id only if you do not
    -- look at what points here. A foreign key may reference a UNIQUE column
    -- SET and nothing else, and `(workspace_id, id)` is the pair that
    -- project_teams, project_milestones and issues all reference. Without it
    -- none of the three composite keys below can be declared at all, and the
    -- schema falls back to single-column keys that permit exactly the
    -- cross-workspace rows this migration exists to refuse.
    CONSTRAINT projects_workspace_id_key UNIQUE (workspace_id, id)
);


-- The many-to-many that makes a project span teams.
--
-- No surrogate id: the row IS its key. An `id UUID PRIMARY KEY` here would
-- need a UNIQUE (workspace_id, project_id, team_id) beside it to stop the same
-- team being added to the same project twice, so it would be a second key that
-- buys nothing and one more column for a mutation to get wrong.
CREATE TABLE project_teams (
    -- One workspace_id for the whole row, and that is the point. Both foreign
    -- keys below read this same column, so the project and the team are
    -- checked against the SAME tenant rather than against two tenants that
    -- happen to be spelled separately. A pair of single-column foreign keys --
    -- project_id -> projects.id, team_id -> teams.id -- would accept a row
    -- pairing workspace A's project with workspace B's team, and nothing in
    -- either constraint would notice.
    workspace_id UUID NOT NULL,

    project_id UUID NOT NULL,
    team_id UUID NOT NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- The primary key is also the uniqueness rule ("a team joins a project
    -- once") and also the index that answers "which teams are on this
    -- project", since (workspace_id, project_id) is its leading prefix.
    CONSTRAINT project_teams_pkey PRIMARY KEY (workspace_id, project_id, team_id),

    -- ON DELETE RESTRICT rather than CASCADE, in both directions.
    --
    -- CASCADE is the tempting choice for a join table and it is the wrong one
    -- here for the reason migrations/002_tenancy.sql gives about issues: a
    -- single `DELETE FROM teams WHERE id = ...` would quietly detach that team
    -- from every project in the workspace while the command tag read
    -- `DELETE 1`. RESTRICT makes the detachment something a caller has to ask
    -- for. ProjectService.delete removes these rows itself, in the same
    -- transaction, before deleting the project -- so the constraint is a guard
    -- on that ordering rather than an obstacle to it.
    CONSTRAINT project_teams_project_fk
        FOREIGN KEY (workspace_id, project_id)
        REFERENCES projects (workspace_id, id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    CONSTRAINT project_teams_team_fk
        FOREIGN KEY (workspace_id, team_id)
        REFERENCES teams (workspace_id, id)
        ON DELETE RESTRICT ON UPDATE RESTRICT
);


CREATE TABLE project_milestones (
    id UUID PRIMARY KEY DEFAULT uuidv7(),

    -- Carried rather than derived through project_id, for the same reason
    -- issues carries one: it is what lets every read be scoped by an equality
    -- on the tenant without a join, and what lets the composite keys below
    -- exist at all.
    workspace_id UUID NOT NULL,
    project_id UUID NOT NULL,

    name TEXT NOT NULL,
    target_date DATE,

    -- Ordering within the project, chosen by the caller.
    --
    -- Deliberately NOT unique per project. Uniqueness sounds tidier and turns
    -- every reorder into a multi-statement shuffle that a non-deferrable
    -- constraint rejects halfway through -- moving a milestone up means
    -- vacating the position it is moving into first. Ties are broken by `id`
    -- wherever milestones are ordered, which makes the order total without
    -- making it fragile.
    position INTEGER NOT NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT project_milestones_position_check
        CHECK (position >= 0),

    CONSTRAINT project_milestones_project_fk
        FOREIGN KEY (workspace_id, project_id)
        REFERENCES projects (workspace_id, id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    -- [FK TARGET] What issues_milestone_fk references. Three columns, not two:
    -- carrying `project_id` into the referenced key is what makes an issue's
    -- milestone have to belong to the issue's own project, rather than merely
    -- to some project in the same workspace.
    CONSTRAINT project_milestones_project_id_key
        UNIQUE (workspace_id, project_id, id)
);


-- Nullable, and staying nullable: most issues belong to no project, which is
-- a real state and not a missing value. No backfill and no SET NOT NULL
-- follow, so nothing here rewrites the existing table -- on PostgreSQL 11+ a
-- nullable ADD COLUMN with no default is a catalog change.
--
-- No DEFAULT on either column. The argument is the one
-- migrations/002_tenancy.sql:102-109 makes about the tenancy columns: a
-- default outlives the migration, so every later insert that forgot to say
-- which project an issue belongs to would silently file it in whichever
-- project the default named.
ALTER TABLE issues ADD COLUMN project_id UUID;
ALTER TABLE issues ADD COLUMN milestone_id UUID;

-- The issue's project must be in the issue's own workspace.
--
-- MATCH SIMPLE (the default) skips the check entirely for a row with any NULL
-- referencing column, which is exactly the behaviour wanted here and only
-- because `issues.workspace_id` is NOT NULL: the sole column that can be NULL
-- is project_id, and an issue with no project is the case being exempted.
ALTER TABLE issues ADD CONSTRAINT issues_project_fk
    FOREIGN KEY (workspace_id, project_id)
    REFERENCES projects (workspace_id, id)
    ON DELETE RESTRICT ON UPDATE RESTRICT;

-- The issue's milestone must belong to the issue's own project -- not just to
-- the same workspace. Three columns are what buys that: a two-column key onto
-- (workspace_id, id) would happily accept a milestone from a different project
-- in the same tenant, which is a mis-filing no client could see and no query
-- would report.
ALTER TABLE issues ADD CONSTRAINT issues_milestone_fk
    FOREIGN KEY (workspace_id, project_id, milestone_id)
    REFERENCES project_milestones (workspace_id, project_id, id)
    ON DELETE RESTRICT ON UPDATE RESTRICT;

-- The hole MATCH SIMPLE leaves, closed.
--
-- issues_milestone_fk is skipped for any row with a NULL among its three
-- referencing columns, and `project_id` is one of them. So without this check
-- a row holding `project_id IS NULL` and a real `milestone_id` passes the
-- foreign key untested: an issue in no project, pointing at a milestone of
-- somebody else's. The check is the cheapest way to say that a milestone is
-- only meaningful inside the project that owns it.
ALTER TABLE issues ADD CONSTRAINT issues_milestone_requires_project
    CHECK (milestone_id IS NULL OR project_id IS NOT NULL);


-- The keyset shape migrations/002_tenancy.sql uses for issues, applied to the
-- one product list this migration adds. `workspace_id` leads because every
-- product query does; (id DESC) breaks a created_at tie, which is what keeps
-- the cursor comparison in ProjectRepository.list total and therefore stable.
CREATE INDEX projects_workspace_created_at_id_idx
    ON projects (workspace_id, created_at DESC, id DESC);

-- The reverse direction of project_teams: "which projects is this team on",
-- and, more importantly, the referencing side of project_teams_team_fk.
-- PostgreSQL indexes the REFERENCED side of a foreign key and never the
-- referencing side, so without this every `DELETE FROM teams` scans
-- project_teams in full to satisfy the RESTRICT check. The forward direction
-- needs no index of its own: (workspace_id, project_id) is the leading prefix
-- of project_teams_pkey.
CREATE INDEX project_teams_workspace_team_idx
    ON project_teams (workspace_id, team_id);

-- Milestones in their display order, and the referencing side of
-- project_milestones_project_fk in the same index. `id` is the tie-break that
-- makes the order total, and it is in the index so the ordering is read rather
-- than sorted.
CREATE INDEX project_milestones_workspace_project_position_idx
    ON project_milestones (workspace_id, project_id, position, id);

-- One index for both new foreign keys on issues: (workspace_id, project_id) is
-- the prefix issues_project_fk's RESTRICT check looks up, and all three
-- columns are what issues_milestone_fk's does. Without it, deleting a project
-- or a milestone scans every issue in the database.
--
-- Not partial on `project_id IS NOT NULL`, though most rows will have none.
-- The predicate a referential-integrity check issues is generated by
-- PostgreSQL rather than written here, so whether it would match a partial
-- index is a property of the planner's implication prover rather than of this
-- schema -- and the cost of being wrong is a full scan of issues on every
-- project deletion. A plain index is the same shape as
-- issues_workspace_team_idx and needs no such reasoning to be correct.
CREATE INDEX issues_workspace_project_milestone_idx
    ON issues (workspace_id, project_id, milestone_id);
