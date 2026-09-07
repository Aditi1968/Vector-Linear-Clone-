-- Initiatives, project health, the updates that report it, and the
-- dependencies between projects.
--
-- migrations/009_projects.sql opens by saying that the shape it exists to make
-- impossible is `projects.team_id` -- a project owned by one team. This file
-- is the layer above that argument: a project is one unit of work, an
-- initiative is the several projects that add up to a goal, and an initiative
-- may itself sit under another one. So the shape THIS file exists to make
-- impossible is `projects.initiative_id`. A project belongs to initiatives
-- through `initiative_projects`, which is `project_teams` with one parent
-- swapped, deliberately down to the column order and the absent surrogate id.
--
-- Four things are added, and they are one feature rather than four:
--
--   * `initiatives`, including the self-reference that makes sub-initiatives
--     possible;
--   * a `health` column on `projects` and on `initiatives` -- the CURRENT
--     value -- beside `project_updates` and `initiative_updates`, which are
--     the LOG. "Health over time" is the log; "how is this project doing" is
--     the column. See the note on `projects.health` for why both exist and
--     what keeps them from drifting;
--   * `project_dependencies`, the blocks / blocked-by edge between two
--     projects.
--
-- Every table below carries `workspace_id` and every foreign key out of it is
-- composite, following the pattern 002 establishes and 009 restates: a join
-- row holds ONE workspace_id and reaches both parents through it, so
-- "initiative in workspace A, project from workspace B" is not a row
-- PostgreSQL will store -- there is no column left for the second workspace
-- to go in.
--
-- ------------------------------------------------------------------
-- What this file does NOT enforce
-- ------------------------------------------------------------------
--
-- Two graphs appear here and neither can be kept acyclic by a constraint, for
-- the reason migrations/010_issue_relations.sql states about sub-issues: no
-- CHECK may read a second row, so A -> A is refusable and A -> B -> A is not.
-- Both guards therefore live in a service, and both are only as strong as the
-- rule that every writer goes through it:
--
--   * the initiative hierarchy. `initiatives_parent_not_self` refuses A -> A.
--     InitiativeService.set_parent takes a per-workspace advisory lock, walks
--     the proposed parent's ancestors and the moving initiative's descendants
--     in ONE bounded recursive statement, and refuses both a cycle and a tree
--     deeper than app.domain.initiatives.MAX_INITIATIVE_DEPTH. The walk is
--     bounded by that depth rather than by the graph, which is what keeps it
--     from being an unbounded recursion over a table an attacker can grow.
--   * the project dependency graph. `project_dependencies_not_self` refuses
--     A blocks A. ProjectService.add_dependency takes the same kind of lock
--     and walks forward from the blocked project to see whether it already
--     reaches the blocking one. That walk is NOT depth-bounded and must not
--     be: a dependency chain has no product limit, and truncating the search
--     would admit exactly the long cycles it exists to refuse. It terminates
--     on `CYCLE ... SET ... USING path` instead, so a graph some other writer
--     has already broken still produces an answer rather than looping.
--
-- Apply this ONLY through `python -m scripts.apply_migration
-- migrations/022_initiatives.sql`. The runner wraps the file in one
-- transaction, takes an advisory lock and records a checksum; a hand-run gets
-- none of that and executes statement-at-a-time in autocommit, which here
-- means `projects` left carrying a `health` column with no CHECK behind it and
-- half the tables that reference it absent.
--
-- DEPENDS ON 004, for `workspace_members` and the (workspace_id, user_id)
-- PRIMARY KEY every owner and author reference below targets, and on 009, for
-- `projects` and its `projects_workspace_id_key`. Applying this file before
-- either fails on the first composite foreign key and -- inside the single
-- transaction the runner wraps the file in -- leaves nothing behind. That
-- failure IS the dependency check: the ledger records what has been applied
-- and not what depends on what, so the schema itself is what has to refuse an
-- out-of-order apply.


CREATE TABLE initiatives (
    id UUID PRIMARY KEY DEFAULT uuidv7(),

    workspace_id UUID NOT NULL,

    name TEXT NOT NULL,
    description TEXT,

    -- TEXT with a CHECK, deliberately not an enum type, for the reason 009
    -- gives at length on `projects.state`: the runner wraps each migration in
    -- one transaction, and PostgreSQL refuses to USE an enum label added by
    -- `ALTER TYPE ... ADD VALUE` in the same transaction that added it. So the
    -- migration that one day adds a fifth status would have to be split across
    -- two files or run outside the runner. A CHECK widens with DROP CONSTRAINT
    -- + ADD CONSTRAINT in one file.
    --
    -- What the CHECK does not buy is ordering or transitions: it says which
    -- statuses exist, not which may follow which.
    status TEXT NOT NULL,

    -- How it is going, as most recently reported. See the long note on
    -- `projects.health` below -- this column and `initiative_updates` are the
    -- current value and the log of the same fact, and that note is the whole
    -- argument for storing both.
    --
    -- Nullable, and NULL is a real state rather than a gap: an initiative
    -- nobody has posted an update on has no health, which is different from
    -- one reported as on track.
    health TEXT,

    -- DATE, not TIMESTAMPTZ, for 009's reason: a target date is a day on a
    -- calendar that people in several timezones agree on, and storing an
    -- instant makes "due the 30th" answer differently either side of midnight
    -- UTC.
    target_date DATE,

    -- Who is accountable, or nobody. `projects.lead_id` under a different
    -- name, and the composite reference below is what makes the name the only
    -- difference.
    owner_id UUID,

    -- The initiative this one sits under, or NULL for a top-level one.
    --
    -- Nullable permanently: "has no parent" is the ordinary state, not a gap
    -- waiting to be backfilled. No DEFAULT -- there is no sensible initiative
    -- to default a parent to, and a default here would attach every future
    -- insert to it.
    --
    -- Nullability is also what makes initiatives_parent_fk behave correctly
    -- rather than dangerously. That key is MATCH SIMPLE and skips its check
    -- entirely for a row with any NULL among its referencing columns; because
    -- `workspace_id` is NOT NULL the only column that can be NULL is this one,
    -- so the exemption is precisely "this initiative has no parent" and cannot
    -- be widened by a NULL arriving in the other half.
    parent_initiative_id UUID,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT initiatives_workspace_fk
        FOREIGN KEY (workspace_id)
        REFERENCES workspaces (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    CONSTRAINT initiatives_status_check
        CHECK (status IN ('planned', 'active', 'completed', 'canceled')),

    -- The same three values `projects_health_check` admits, written out twice
    -- rather than shared. There is no way to share a CHECK expression between
    -- two tables short of a domain type or a function, and both put the
    -- vocabulary somewhere a reader of either table cannot see it.
    -- tests/test_migration_022_db.py asserts the two agree.
    CONSTRAINT initiatives_health_check
        CHECK (health IS NULL OR health IN ('on_track', 'at_risk', 'off_track')),

    CONSTRAINT initiatives_name_length
        CHECK (length(name) BETWEEN 1 AND 200),

    -- The owner must be a member of THIS initiative's workspace.
    --
    -- `REFERENCES users (id)` is the obvious spelling and it is the bug, for
    -- the reason 009 spells out on projects_lead_fk: it would check that the
    -- owner is a real account somewhere and say nothing about where, so any
    -- user id from any tenant would be accepted. The reference is composite
    -- onto `workspace_members (workspace_id, user_id)` -- that table's PRIMARY
    -- KEY, marked [FK TARGET] in 004 for exactly this -- and one workspace_id
    -- column feeds both this constraint and initiatives_workspace_fk.
    --
    -- ON DELETE RESTRICT: removing someone from a workspace while they still
    -- own an initiative is refused rather than silently vacating it.
    CONSTRAINT initiatives_owner_fk
        FOREIGN KEY (workspace_id, owner_id)
        REFERENCES workspace_members (workspace_id, user_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    -- Self-parenting, refused by the server.
    --
    -- IS DISTINCT FROM rather than `<>`, copying 010's issues_parent_not_self
    -- and its reasoning: a plain inequality against a NULL parent evaluates to
    -- NULL, a CHECK treats NULL as satisfied, and the two therefore agree here
    -- only by accident -- an accident that stops holding the moment this
    -- expression grows a second conjunct.
    CONSTRAINT initiatives_parent_not_self
        CHECK (parent_initiative_id IS DISTINCT FROM id),

    -- The parent must be an initiative in the SAME workspace.
    --
    -- A self-reference declared inside the CREATE TABLE, which PostgreSQL
    -- accepts: the table exists by the time the constraint is checked. It
    -- targets initiatives_workspace_id_key below rather than the primary key,
    -- because a single-column reference to `id` would let an initiative in
    -- workspace A hang under a parent in workspace B with every constraint
    -- reporting success.
    --
    -- ON DELETE RESTRICT, and here that is the reason 010 gives turned around:
    -- CASCADE would make one `DELETE FROM initiatives` delete the entire tree
    -- beneath it, recursively, while the command tag read `DELETE 1`.
    -- InitiativeService.delete detaches the children itself, in the same
    -- transaction, so the constraint is a guard on that ordering rather than
    -- an obstacle to it.
    --
    -- This key refuses a cross-tenant parent and a parent that does not exist.
    -- It does NOT refuse a cycle of length two or more -- see the block at the
    -- top of this file for where that guard lives and exactly how strong it is.
    CONSTRAINT initiatives_parent_fk
        FOREIGN KEY (workspace_id, parent_initiative_id)
        REFERENCES initiatives (workspace_id, id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    -- [FK TARGET] Redundant beside the primary key on id only if you do not
    -- look at what points here. A foreign key may reference a UNIQUE column
    -- SET and nothing else, and `(workspace_id, id)` is the pair that
    -- initiatives_parent_fk, initiative_projects and initiative_updates all
    -- reference. Without it none of the three can be declared at all, and the
    -- schema falls back to single-column keys that permit exactly the
    -- cross-workspace rows this migration exists to refuse.
    CONSTRAINT initiatives_workspace_id_key UNIQUE (workspace_id, id)
);


-- The many-to-many that makes a project part of an initiative.
--
-- `project_teams` from 009 with `teams` swapped for `initiatives`, and every
-- decision in that table's comment applies here unchanged: no surrogate id,
-- because the row IS its key and an `id UUID PRIMARY KEY` would need a UNIQUE
-- beside it to stop the same project being added twice -- a second key that
-- buys nothing and one more column for a mutation to get wrong.
CREATE TABLE initiative_projects (
    -- One workspace_id for the whole row, and that is the point. Both foreign
    -- keys below read this same column, so the initiative and the project are
    -- checked against the SAME tenant rather than against two that happen to
    -- be spelled separately. A pair of single-column foreign keys --
    -- initiative_id -> initiatives.id, project_id -> projects.id -- would
    -- accept a row pairing workspace A's initiative with workspace B's
    -- project, and nothing in either constraint would notice.
    workspace_id UUID NOT NULL,

    initiative_id UUID NOT NULL,
    project_id UUID NOT NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- The primary key is also the uniqueness rule ("a project joins an
    -- initiative once") and also the index that answers "which projects are in
    -- this initiative", since (workspace_id, initiative_id) is its leading
    -- prefix.
    CONSTRAINT initiative_projects_pkey
        PRIMARY KEY (workspace_id, initiative_id, project_id),

    -- ON DELETE RESTRICT rather than CASCADE, in both directions, for the
    -- reason 009 gives about project_teams: a single
    -- `DELETE FROM projects WHERE id = ...` would quietly detach that project
    -- from every initiative in the workspace while the command tag read
    -- `DELETE 1`. The services remove these rows themselves, in the same
    -- transaction, before deleting either parent.
    CONSTRAINT initiative_projects_initiative_fk
        FOREIGN KEY (workspace_id, initiative_id)
        REFERENCES initiatives (workspace_id, id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    CONSTRAINT initiative_projects_project_fk
        FOREIGN KEY (workspace_id, project_id)
        REFERENCES projects (workspace_id, id)
        ON DELETE RESTRICT ON UPDATE RESTRICT
);


-- How a project is going, as most recently reported.
--
-- Nullable, and NULL is a real state: a project nobody has posted an update on
-- has no health, which is a different fact from one reported as on track. No
-- DEFAULT, for the argument 002 makes about the tenancy columns and 009
-- repeats about `state` -- a default outlives the migration, so every later
-- insert that never mentioned health would silently claim whichever value the
-- default named.
--
-- ------------------------------------------------------------------
-- Why this column exists at all, beside `project_updates`
-- ------------------------------------------------------------------
--
-- It is denormalised: the same fact is in the newest `project_updates` row.
-- The honest alternative is to derive it -- a correlated
-- `ORDER BY created_at DESC LIMIT 1` per project -- which is one index lookup
-- per row and needs no column, and which was rejected for two reasons.
--
-- The first is that health is a property of the PROJECT in the product, not of
-- the update. A board colours a project red; it does not colour it "red as of
-- the update of the 3rd". Deriving it would make the column list of `projects`
-- disagree with what the product says a project has.
--
-- The second is that deriving it makes "set the health" impossible without
-- also writing a body, which is a product rule this schema would be inventing.
--
-- What keeps the two in step is a transaction, not a trigger:
-- ProjectService.post_update writes the log row and stamps this column in one
-- transaction, so there is no committed state in which they disagree. A
-- trigger would enforce it against writers that bypass the service too, and is
-- deliberately not used here -- 010 makes the same call about the sub-issue
-- guard, and the same caveat applies: a bulk import or a hand-run UPDATE can
-- write a health this column does not match, and nothing here will notice. The
-- log is authoritative if they ever differ.
ALTER TABLE projects ADD COLUMN health TEXT;

-- The same three values initiatives_health_check admits. Written out rather
-- than shared for the reason given there.
ALTER TABLE projects ADD CONSTRAINT projects_health_check
    CHECK (health IS NULL OR health IN ('on_track', 'at_risk', 'off_track'));


-- One posted update on one project: what the health was, and why.
--
-- Append-only by shape rather than by permission. There is no `updated_at` and
-- no edit path: an update is a statement somebody made at a moment, and
-- rewriting it would rewrite the history the column above is a summary of. The
-- same argument 010 makes for `issue_relations` having no updated_at.
CREATE TABLE project_updates (
    id UUID PRIMARY KEY DEFAULT uuidv7(),

    -- Denormalised from the project solely so that the two foreign keys below
    -- can be composite, and therefore so a read can be scoped by tenant
    -- without joining `projects`. The value is not independent -- the project
    -- key forces it to equal the project's own workspace -- so it cannot
    -- drift. The same argument 007 makes about `comments.workspace_id`.
    workspace_id UUID NOT NULL,
    project_id UUID NOT NULL,

    -- NOT NULL, unlike `projects.health`. An update whose whole purpose is to
    -- report health with no health in it is not a row worth storing, and
    -- allowing one would make the log unable to answer the question the column
    -- above is derived from.
    health TEXT NOT NULL,

    body TEXT NOT NULL,

    -- NOT NULL, matching `comments.author_id` in 007 and for its reason: an
    -- update is somebody's statement, and an unattributed one is not a record
    -- of anything. The composite reference below is what stops it being
    -- anybody's statement -- see comments_author_fk in 007 for the
    -- impersonation a single-column key to `users (id)` would accept.
    author_id UUID NOT NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT project_updates_project_fk
        FOREIGN KEY (workspace_id, project_id)
        REFERENCES projects (workspace_id, id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    -- ON DELETE RESTRICT: a member who has posted an update cannot be removed
    -- from the workspace until their updates are reassigned or deleted. That
    -- is the correct refusal to make loudly; CASCADE would destroy a project's
    -- history as a side effect of an administrative removal.
    CONSTRAINT project_updates_author_fk
        FOREIGN KEY (workspace_id, author_id)
        REFERENCES workspace_members (workspace_id, user_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    CONSTRAINT project_updates_health_check
        CHECK (health IN ('on_track', 'at_risk', 'off_track')),

    -- A bound, not a validation. The body is rendered back to clients, so its
    -- content is an escaping question rather than a length one -- but an
    -- unbounded TEXT column is a way to make one row megabytes wide, and
    -- 10000 is far above any update a person writes.
    CONSTRAINT project_updates_body_length
        CHECK (length(body) BETWEEN 1 AND 10000)
);


-- The initiative-side twin of project_updates. Two tables rather than one
-- polymorphic table, and the reason is the tenancy mechanism rather than
-- taste: a single `parent_type`/`parent_id` pair cannot be the referencing
-- half of a composite foreign key, so a shared table would have to fall back
-- to no foreign key at all -- which is precisely the cross-tenant row every
-- other table in this file is shaped to refuse. 017 splits
-- github_pull_request_issues from github_commit_issues for the same reason.
CREATE TABLE initiative_updates (
    id UUID PRIMARY KEY DEFAULT uuidv7(),

    workspace_id UUID NOT NULL,
    initiative_id UUID NOT NULL,

    health TEXT NOT NULL,
    body TEXT NOT NULL,
    author_id UUID NOT NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT initiative_updates_initiative_fk
        FOREIGN KEY (workspace_id, initiative_id)
        REFERENCES initiatives (workspace_id, id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    CONSTRAINT initiative_updates_author_fk
        FOREIGN KEY (workspace_id, author_id)
        REFERENCES workspace_members (workspace_id, user_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    CONSTRAINT initiative_updates_health_check
        CHECK (health IN ('on_track', 'at_risk', 'off_track')),

    CONSTRAINT initiative_updates_body_length
        CHECK (length(body) BETWEEN 1 AND 10000)
);


-- One project blocks another.
--
-- ONE ROW PER EDGE, stored in one direction only, which is the decision the
-- rest of the table is shaped by. "A blocks B" and "B is blocked by A" are the
-- same edge read from its two ends, exactly as 010 argues about `blocks` on
-- issues -- so `blocked_by` is never stored and is produced on the way out by
-- reading the other column. Storing both directions would mean a unique
-- constraint that accepted (A, B) and (B, A) as different rows because they
-- are different tuples, while being the same dependency.
--
-- Unlike `issue_relations` this edge is DIRECTED and has no symmetric
-- counterpart, so there is no canonicalising order to impose and nothing here
-- corresponds to issue_relations_symmetric_ordered.
--
-- No surrogate id: the row IS its key, as project_teams and
-- initiative_projects.
CREATE TABLE project_dependencies (
    -- One workspace_id, read by BOTH foreign keys below. That is the entire
    -- cross-tenant guarantee -- the same sentence initiative_projects makes,
    -- and it matters more here because both ends are the same table: a pair of
    -- single-column keys onto `projects (id)` would each pass while together
    -- describing a dependency in workspace A between a project in A and a
    -- project in B, with every constraint reporting success.
    workspace_id UUID NOT NULL,

    blocking_project_id UUID NOT NULL,
    blocked_project_id UUID NOT NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- No updated_at, deliberately, and no `type`. A dependency has no mutable
    -- field: changing either end makes it a different dependency, so the
    -- operations are create and delete. A column that could never differ from
    -- created_at would be a standing invitation to add an update path this key
    -- has not been designed for.

    -- The key is also the uniqueness rule ("one project blocks another once")
    -- and also the index answering "what does this project block", since
    -- (workspace_id, blocking_project_id) is its leading prefix.
    CONSTRAINT project_dependencies_pkey
        PRIMARY KEY (workspace_id, blocking_project_id, blocked_project_id),

    -- A project cannot block itself. Both columns are NOT NULL, so this is a
    -- plain inequality with no NULL case to reason about -- unlike
    -- initiatives_parent_not_self above, where the parent may be absent.
    --
    -- This refuses the one-step cycle and nothing longer; see the top of this
    -- file for where the multi-step guard lives.
    CONSTRAINT project_dependencies_not_self
        CHECK (blocking_project_id <> blocked_project_id),

    CONSTRAINT project_dependencies_blocking_fk
        FOREIGN KEY (workspace_id, blocking_project_id)
        REFERENCES projects (workspace_id, id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    CONSTRAINT project_dependencies_blocked_fk
        FOREIGN KEY (workspace_id, blocked_project_id)
        REFERENCES projects (workspace_id, id)
        ON DELETE RESTRICT ON UPDATE RESTRICT
);


-- The keyset shape 002 uses for issues and 009 for projects, applied to the
-- one product list this migration adds. `workspace_id` leads because every
-- product query does; (id DESC) breaks a created_at tie, which is what keeps
-- the cursor comparison in InitiativeRepository.list total and therefore
-- stable.
CREATE INDEX initiatives_workspace_created_at_id_idx
    ON initiatives (workspace_id, created_at DESC, id DESC);

-- The referencing side of initiatives_owner_fk, and the answer to "which
-- initiatives does this person own".
--
-- PostgreSQL indexes the REFERENCED side of a foreign key and never the
-- referencing side, so without this every deletion from `workspace_members`
-- scans `initiatives` in full to satisfy the RESTRICT above -- on the path of
-- every member removal in every workspace.
CREATE INDEX initiatives_workspace_owner_idx
    ON initiatives (workspace_id, owner_id);

-- Two readers, one index: "this initiative's children, newest first" and the
-- referencing side of initiatives_parent_fk.
--
-- NOT partial on `parent_initiative_id IS NOT NULL`, though most initiatives
-- will have no parent -- which is the opposite of what 010 chose for
-- issues_workspace_parent_created_at_id_idx. 009 gives the argument this file
-- follows: the predicate a referential-integrity check issues is generated by
-- PostgreSQL rather than written here, so whether it matches a partial index
-- is a property of the planner's implication prover rather than of this
-- schema, and the cost of being wrong is a full scan of `initiatives` on every
-- initiative deletion. A plain index needs no such reasoning to be correct.
CREATE INDEX initiatives_workspace_parent_created_at_id_idx
    ON initiatives (workspace_id, parent_initiative_id, created_at DESC, id DESC);

-- The reverse direction of initiative_projects: "which initiatives is this
-- project in", and, more importantly, the referencing side of
-- initiative_projects_project_fk -- without which every `DELETE FROM projects`
-- scans the join table in full to satisfy its RESTRICT. The forward direction
-- needs no index of its own: (workspace_id, initiative_id) is the leading
-- prefix of initiative_projects_pkey.
CREATE INDEX initiative_projects_workspace_project_idx
    ON initiative_projects (workspace_id, project_id);

-- The update history of one project, newest first -- which is both the product
-- read and the lookup `projects.health` is reconciled against -- and the
-- referencing side of project_updates_project_fk in the same index. `id DESC`
-- is the tie-break that makes the order total, which matters more here than
-- usual: two updates posted in one transaction share a `created_at`, and
-- "which is the latest" must not depend on the scan order.
CREATE INDEX project_updates_workspace_project_created_at_id_idx
    ON project_updates (workspace_id, project_id, created_at DESC, id DESC);

-- The referencing side of project_updates_author_fk. Without it every member
-- removal scans `project_updates` in full.
CREATE INDEX project_updates_workspace_author_idx
    ON project_updates (workspace_id, author_id);

CREATE INDEX initiative_updates_workspace_initiative_created_at_id_idx
    ON initiative_updates (workspace_id, initiative_id, created_at DESC, id DESC);

CREATE INDEX initiative_updates_workspace_author_idx
    ON initiative_updates (workspace_id, author_id);

-- "What blocks this project", the reverse of the primary key's direction, and
-- the referencing side of project_dependencies_blocked_fk. Both halves of a
-- dependency read land on an index: the forward direction on
-- project_dependencies_pkey's leading columns, the reverse on this one. The
-- cycle walk in ProjectRepository follows the forward direction and therefore
-- needs only the primary key.
CREATE INDEX project_dependencies_workspace_blocked_idx
    ON project_dependencies (workspace_id, blocked_project_id);
