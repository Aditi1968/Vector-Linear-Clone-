-- Team keys, per-team issue numbering, and team-scoped workflow states.
--
-- 002 gave every issue an owner. This gives the product the three things a
-- Linear-shaped issue tracker needs before it can show an issue to anyone:
-- a short identifier a human can say out loud (ENG-42), a status that is a
-- row rather than a string in application code, and -- the part that is
-- easy to get wrong quietly -- a way to allocate the number in ENG-42 that
-- two simultaneous creations cannot both win.
--
-- Apply this ONLY through `python -m scripts.apply_migration
-- migrations/005_team_workflows.sql`. Everything below is ordered so that a
-- failure anywhere leaves the schema exactly as it was, and that property
-- comes entirely from the single transaction the runner wraps the file in.
-- Run statement-at-a-time in autocommit, a failure partway through leaves
-- issues carrying nullable `number` and `workflow_state_id` columns, a
-- backfill that ran for some rows, a counter that no longer agrees with the
-- numbers already handed out, and no constraint anywhere to say so.
--
-- The file assumes 002's shape and nothing more: `teams` as 002 created it,
-- `issues` carrying 002's two NOT NULL tenancy columns, and the bootstrap
-- tenant 002 seeded. It does not assume `issues` is empty, and it does not
-- assume `teams` holds only the bootstrap row -- see the two backfills.


-- --------------------------------------------------------------------------
-- Team keys
-- --------------------------------------------------------------------------

-- Nullable, then backfilled, then constrained, for the reason 002 sets out at
-- length: ADD COLUMN ... NOT NULL without a DEFAULT is validated against every
-- existing row as it runs, and no backfill can precede it because the column
-- does not exist until that statement finishes. A DEFAULT would dodge that and
-- would be worse -- every team created afterwards would silently share one key.
ALTER TABLE teams ADD COLUMN key TEXT;

-- The backfill is written to be correct for any `teams` this migration can
-- meet, not only for the one row 002 seeds.
--
-- 002 creates exactly one team and no application code creates a second, so in
-- practice this UPDATE touches one row and gives it 'CORE'. Writing it as
-- `UPDATE teams SET key = 'CORE'` would be shorter and would also be a claim --
-- "there is exactly one team" -- that this file has no way to check and that
-- would fail, if it were ever false, as a unique violation with no explanation
-- attached. The generated form is total: every workspace's teams get distinct
-- keys, whatever the table holds.
--
-- 'CORE' can never collide with a generated 'TEAM<n>', and row_number() is
-- distinct within each PARTITION BY workspace_id, which is exactly the scope
-- teams_workspace_key_unique below requires uniqueness over.
--
-- A workspace holding more than 999,999 teams would generate a key too long
-- for teams_key_format and abort the migration. That is the correct failure:
-- the alternative is silently truncating a tenant's team keys into collisions.
UPDATE teams
SET key = assigned.key
FROM (
    SELECT
        id,
        CASE
            WHEN id = '00000000-0000-7000-8000-000000000002'::UUID THEN 'CORE'
            ELSE 'TEAM' || row_number() OVER (
                PARTITION BY workspace_id ORDER BY created_at, id
            )
        END AS key
    FROM teams
) AS assigned
WHERE teams.id = assigned.id;

ALTER TABLE teams ALTER COLUMN key SET NOT NULL;

-- Uppercase-only, and for the same reason workspaces_slug_format is
-- lowercase-only: UNIQUE on plain TEXT is case-sensitive, so without this
-- 'Eng' and 'ENG' are two different teams in one workspace that both insert
-- cleanly, and an identifier printed as ENG-42 no longer names one issue.
--
-- No hyphen, and that exclusion is load-bearing rather than cosmetic. The
-- identifier is rendered `<key>-<number>`, so a key containing a hyphen makes
-- the rendering ambiguous: 'EN-G' with number 42 and 'EN' with number... there
-- is no parse of `EN-G-42` that a reader or a parser can settle. Leading digits
-- are excluded for the mirror-image reason on the other end.
ALTER TABLE teams ADD CONSTRAINT teams_key_format
    CHECK (key ~ '^[A-Z][A-Z0-9]{0,9}$');

-- Unique per workspace, emphatically not globally. Two tenants both wanting
-- ENG is the normal case, not a conflict: their issues are ENG-1 in different
-- workspaces and nothing about either is ambiguous, because every lookup that
-- resolves a key is already scoped to a workspace. A global UNIQUE (key) would
-- make one tenant's choice of team key deny it to every other tenant, which is
-- both a product defect and an information leak -- a failed create would tell
-- an unrelated workspace that some other workspace holds that key.
--
-- Named `_unique` rather than PostgreSQL's `_key` suffix only because the
-- column is itself called `key`: `teams_workspace_key_key` reads as a typo.
ALTER TABLE teams ADD CONSTRAINT teams_workspace_key_unique
    UNIQUE (workspace_id, key);


-- --------------------------------------------------------------------------
-- The issue number counter
-- --------------------------------------------------------------------------

-- The allocator behind ENG-1, ENG-2, ENG-3.
--
-- Issue numbers are per team and sequential, so something has to decide which
-- number a new issue gets, and that decision is made concurrently by definition
-- -- two people can file against the same team in the same millisecond. Three
-- ways to make it, and only the third is safe here:
--
--   * `SELECT max(number) + 1 FROM issues WHERE team_id = $1`. A plain SELECT
--     takes no lock, so two transactions read the same maximum and compute the
--     same next number. Under READ COMMITTED both succeed at computing it; one
--     then fails on issues_team_number_key, which turns an ordinary concurrent
--     create into a user-visible error. This is the shape this column exists to
--     make unnecessary, and it is forbidden.
--   * A PostgreSQL sequence per team. Sequences are non-transactional -- that
--     is what makes them fast -- so nextval() hands out numbers a rollback
--     never returns, and every failed create leaves a permanent hole in the
--     team's numbering. It also needs DDL (CREATE SEQUENCE) at runtime, once
--     per team, which puts unbounded catalog growth on the team-creation path.
--   * A counter column, incremented with UPDATE ... RETURNING inside the
--     creating transaction. This one.
--
-- `UPDATE teams SET issue_counter = issue_counter + 1 WHERE ... RETURNING
-- issue_counter` takes a row-level exclusive lock on the team as it runs and
-- holds it until the transaction ends. A second allocation for the same team
-- blocks on that lock; when the first commits, PostgreSQL re-reads the row it
-- was blocked on and re-evaluates `issue_counter + 1` against the *committed*
-- value rather than the snapshot the second transaction started with. So two
-- concurrent allocations can never receive the same number, and every
-- allocation that commits is greater than every allocation committed before
-- it, under any number of concurrent creations. (Under REPEATABLE READ or
-- SERIALIZABLE the blocked statement raises a serialization failure instead of
-- proceeding -- also safe, and never a duplicate.)
--
-- It is also gapless, and that is the property a sequence cannot offer. This
-- is an ordinary column, so the increment is transactional like everything
-- else: a transaction that allocates 7 and then rolls back takes the
-- increment down with it, the counter goes back to 6, and the next allocation
-- is handed 7 again. Numbers are therefore reused after a failure rather than
-- burnt -- which is why the *returned* values across a run containing
-- rollbacks are not globally increasing, while the numbers that end up
-- committed are exactly 1..n with no holes.
--
-- Gaplessness is conditional on the usage contract, and the condition is worth
-- stating because it is easy to break: every transaction that allocates must
-- insert exactly one issue carrying that number, and must fail as a whole if
-- the insert fails. Allocate a number and commit without using it -- or catch
-- the insert's error and commit anyway -- and the hole is permanent.
--
-- The cost is honest and worth stating: allocation serialises creation within
-- one team for the remainder of the transaction that allocates. So the
-- allocation belongs as late in that transaction as possible, and the
-- transaction should be short. It does not serialise across teams, and it does
-- not touch reads.
--
-- BIGINT rather than INTEGER, on no stronger grounds than that four extra
-- bytes on a table with one row per team is nothing, and a bound nobody has
-- to think about is worth more than the bytes. INTEGER would also do: the
-- counter tracks committed allocations, so it never exceeds the number of
-- issues the team has ever had.
--
-- DEFAULT 0 is permanent and correct here, unlike the tenancy defaults 002
-- argues against: a team that has never had an issue has allocated nothing,
-- and the first UPDATE ... RETURNING against it must return 1.
ALTER TABLE teams ADD COLUMN issue_counter BIGINT NOT NULL DEFAULT 0;

ALTER TABLE teams ADD CONSTRAINT teams_issue_counter_non_negative
    CHECK (issue_counter >= 0);


-- --------------------------------------------------------------------------
-- Workflow states
-- --------------------------------------------------------------------------

-- Team-scoped statuses. The rows are the source of truth for what a team's
-- board shows; `type` is the fixed vocabulary application code branches on.
--
-- The split matters more than it looks. A team may rename 'Todo' to 'Up Next',
-- add a second started state called 'In Review', or delete one entirely --
-- none of which any code should notice, because no code should ever compare a
-- state's *name*. What code needs to know is whether an issue is started, and
-- that is `type`, which is closed, constrained, and not the user's to change.
CREATE TABLE workflow_states (
    id UUID PRIMARY KEY DEFAULT uuidv7(),

    -- Both halves of the tenant pair, not just team_id. Carrying
    -- workspace_id looks redundant -- a team determines its workspace -- and
    -- is what lets issues_workflow_state_fk below reference
    -- (workspace_id, team_id, id) as one key, so that an issue's workspace,
    -- its team and its state are checked as a single tuple by the server
    -- instead of as three separate facts by application code.
    workspace_id UUID NOT NULL,
    team_id UUID NOT NULL,

    name TEXT NOT NULL,
    type TEXT NOT NULL,

    -- Display order within the team's board, ascending.
    --
    -- Deliberately NOT unique per team. A unique constraint here would make
    -- the ordinary reorder -- swap two adjacent states -- impossible in a
    -- single statement, since the intermediate state of any swap collides;
    -- every reorder would need a temporary out-of-band value or a DEFERRABLE
    -- constraint, and both are complexity bought to protect a display hint.
    -- Ties are broken by id, so `ORDER BY position, id` is total regardless.
    position INTEGER NOT NULL,

    -- Optional: a team that has not chosen colours has none, and NULL says
    -- that. An empty string or a hardcoded grey would both be this column
    -- claiming a choice nobody made.
    color TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- The tenant pair must be a real team's pair. RESTRICT on both sides for
    -- the reasons 002 gives for issues_team_fk: under CASCADE a single
    -- `DELETE FROM teams` would take the team's whole workflow with it (and
    -- then fail anyway against the issues pointing at those states, which is
    -- the good outcome arrived at by luck rather than by design).
    CONSTRAINT workflow_states_team_fk
        FOREIGN KEY (workspace_id, team_id)
        REFERENCES teams (workspace_id, id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    -- The closed vocabulary. A CHECK rather than an enum type: adding a
    -- category later is then an ordinary ALTER inside a migration's
    -- transaction, where `ALTER TYPE ... ADD VALUE` is restricted in ways
    -- that are easy to discover at the wrong moment.
    --
    -- 'canceled' is spelled with one L throughout -- schema, application and
    -- API. One spelling, chosen once, because the failure mode of two is a
    -- CHECK violation on a value that looks correct to whoever wrote it.
    CONSTRAINT workflow_states_type_check
        CHECK (type IN ('backlog', 'unstarted', 'started', 'completed', 'canceled')),

    -- A blank name is not a name. Without this a state can be created with ''
    -- or '   ', which renders as an empty row in every list that shows it and
    -- is indistinguishable from a rendering bug.
    CONSTRAINT workflow_states_name_present
        CHECK (btrim(name) <> ''),

    CONSTRAINT workflow_states_position_non_negative
        CHECK (position >= 0),

    -- Lowercase six-digit hex, or nothing. Narrow on purpose: this value is
    -- interpolated into CSS by the frontend, so the set of things it may
    -- contain is a question about what the browser will do with it, not a
    -- question of formatting taste.
    CONSTRAINT workflow_states_color_format
        CHECK (color ~ '^#[0-9a-f]{6}$'),

    -- Two states called 'Todo' on one team is a UI nobody can use. Scoped to
    -- the team, so two teams may each have a 'Todo'.
    CONSTRAINT workflow_states_team_name_key UNIQUE (team_id, name),

    -- [FK TARGET] What issues_workflow_state_fk references. A foreign key may
    -- only reference a UNIQUE-constrained column set, so without this the
    -- composite key below cannot be declared at all. It also supplies the
    -- index on (workspace_id, team_id) that workflow_states_team_fk's RESTRICT
    -- check needs on this, the referencing, side -- PostgreSQL indexes only
    -- the referenced side of a foreign key automatically.
    CONSTRAINT workflow_states_team_id_key UNIQUE (workspace_id, team_id, id)
);


-- A default workflow for every team that exists, one row per category.
--
-- Seeded from `teams` rather than written as five literals against the
-- bootstrap team, for the same reason the key backfill is generated: this file
-- must leave every team in the database with a usable board, and the
-- workflow_state_id backfill below depends on every team having one state of
-- each of the two categories it maps onto.
--
-- The names here are defaults a team may rename freely. Nothing in the
-- application may look them up by name; see the note on `type` above.
INSERT INTO workflow_states (workspace_id, team_id, name, type, position, color)
SELECT
    teams.workspace_id,
    teams.id,
    seed.name,
    seed.type,
    seed.position,
    seed.color
FROM teams
CROSS JOIN (
    VALUES
        ('Backlog', 'backlog', 0, '#bec2c8'),
        ('Todo', 'unstarted', 1, '#e2e2e2'),
        ('In Progress', 'started', 2, '#f2c94c'),
        ('Done', 'completed', 3, '#5e6ad2'),
        ('Canceled', 'canceled', 4, '#95a2b3')
) AS seed (name, type, position, color);


-- --------------------------------------------------------------------------
-- issues.number
-- --------------------------------------------------------------------------

ALTER TABLE issues ADD COLUMN number BIGINT;

-- Existing issues are numbered in the order they were created, per team.
--
-- `ORDER BY created_at, id` rather than `ORDER BY created_at`: nothing stops
-- two issues sharing a timestamp to the microsecond, and the 002 suite already
-- seeds a pair that does. row_number() over a non-total order picks among tied
-- rows arbitrarily -- which would still produce unique numbers, but would make
-- the numbering non-reproducible, so that running this migration against two
-- copies of the same database could give the same issue different identifiers.
-- id breaks every tie.
--
-- Deliberately unguarded, as 002's backfill is: `number` was created two
-- statements ago inside this transaction, so every row this statement can see
-- carries NULL, and a `WHERE number IS NULL` would be a predicate that can
-- never be false.
UPDATE issues
SET number = numbered.number
FROM (
    SELECT
        id,
        row_number() OVER (
            PARTITION BY team_id ORDER BY created_at, id
        ) AS number
    FROM issues
) AS numbered
WHERE issues.id = numbered.id;

ALTER TABLE issues ALTER COLUMN number SET NOT NULL;

ALTER TABLE issues ADD CONSTRAINT issues_number_positive
    CHECK (number >= 1);

-- The constraint that makes the counter's guarantee visible to the database
-- rather than resting on the application always using it. Two issues on one
-- team with the same number is exactly the corruption a MAX(number)+1
-- allocator produces under concurrency, and this is what makes that failure a
-- refused INSERT instead of two issues both called ENG-42.
--
-- Keyed on (team_id, number) rather than (workspace_id, team_id, number): a
-- team belongs to exactly one workspace and issues_team_fk already forces an
-- issue's workspace to be its team's, so the wider key would constrain nothing
-- extra while making every uniqueness check read three columns.
ALTER TABLE issues ADD CONSTRAINT issues_team_number_key
    UNIQUE (team_id, number);

-- The counter is moved past every number already handed out.
--
-- Without this statement the migration is worse than useless: every team's
-- counter would still read 0 while its issues held 1..n, so the first issue
-- created after the migration would allocate 1 and collide with the oldest
-- issue on the team. Teams with no issues are not in this result at all and
-- keep the DEFAULT 0, which is already the right answer for them.
UPDATE teams
SET issue_counter = allocated.high_water
FROM (
    SELECT team_id, max(number) AS high_water
    FROM issues
    GROUP BY team_id
) AS allocated
WHERE teams.id = allocated.team_id;


-- --------------------------------------------------------------------------
-- issues.workflow_state_id
-- --------------------------------------------------------------------------

ALTER TABLE issues ADD COLUMN workflow_state_id UUID;

-- Every existing issue is placed on its own team's board, using the one piece
-- of status information 001 gave it: `completed_at`.
--
-- Mapping to a category rather than to a name is the same discipline the rest
-- of this file asks of the application: 'Done' is a default a team may rename,
-- 'completed' is not. The seed above puts exactly one state of each category on
-- every team, so this join matches exactly one row per issue -- and if it ever
-- matched none, the SET NOT NULL two statements down aborts the migration
-- rather than leaving an issue with no status.
UPDATE issues
SET workflow_state_id = state.id
FROM workflow_states AS state
WHERE state.team_id = issues.team_id
    AND state.type = CASE
        WHEN issues.completed_at IS NULL THEN 'unstarted'
        ELSE 'completed'
    END;

-- NOT NULL, and this one is not a matter of product preference.
--
-- A composite foreign key is MATCH SIMPLE by default, which skips the check
-- entirely for any row where a referencing column is NULL. issues.workspace_id
-- and issues.team_id are already NOT NULL (002), so a nullable
-- workflow_state_id would be the one column able to switch
-- issues_workflow_state_fk off -- and a NULL there would disable not just the
-- state check but the whole tuple check for that row, while the constraint
-- went on reading as enforced.
ALTER TABLE issues ALTER COLUMN workflow_state_id SET NOT NULL;

-- The tenant-integrity constraint this section exists for: an issue's state
-- must belong to the issue's own team, in the issue's own workspace. Three
-- separate single-column keys would each be satisfied by an issue in workspace
-- A, on team T, sitting in a workflow state belonging to team U in workspace B.
--
-- RESTRICT on both sides: deleting a workflow state that issues are sitting in
-- must fail loudly and be resolved by moving those issues, not by silently
-- nulling their status (impossible here in any case -- the column is NOT NULL)
-- or by deleting them.
ALTER TABLE issues ADD CONSTRAINT issues_workflow_state_fk
    FOREIGN KEY (workspace_id, team_id, workflow_state_id)
    REFERENCES workflow_states (workspace_id, team_id, id)
    ON DELETE RESTRICT ON UPDATE RESTRICT;

-- PostgreSQL indexes the referenced side of a foreign key and not the
-- referencing side, so without this every `DELETE FROM workflow_states` scans
-- issues in full to satisfy the RESTRICT check above. The column order is the
-- FK's column list in the FK's order, because that is the lookup the
-- referential-integrity check performs; it also serves the board query
-- ("issues in this state, for this team") that the same order happens to suit.
--
-- 002's issues_workspace_team_idx is its first two columns and is left alone:
-- it is the narrower index, and it is the one issues_team_fk's own RESTRICT
-- check reads on every `DELETE FROM teams`.
CREATE INDEX issues_workflow_state_idx
    ON issues (workspace_id, team_id, workflow_state_id);

-- There is deliberately no separate index for the identifier lookup. `ENG-42`
-- resolves to a team by (workspace_id, key), which teams_workspace_key_unique
-- indexes, and then to an issue by (team_id, number), which
-- issues_team_number_key indexes. `ORDER BY number DESC` within one team is
-- served by that same index read backwards -- PostgreSQL scans a btree in
-- either direction -- so a second copy in descending order would buy write
-- amplification on every insert in exchange for nothing.

