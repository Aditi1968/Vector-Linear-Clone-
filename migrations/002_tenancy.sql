-- Tenancy: workspaces and teams, with every existing issue backfilled into a
-- bootstrap tenant before either of its new columns is allowed to be non-null.
--
-- 001 created issues owned by nobody. What follows gives every existing row an
-- owner and then makes ownerlessness unrepresentable -- including the
-- half-owned shape a pair of single-column foreign keys would still permit,
-- where an issue claims one workspace while pointing at a team belonging to
-- another.
--
-- Apply this ONLY through `python -m scripts.apply_migration
-- migrations/002_tenancy.sql`. A hand-run of this file -- pasted into a web
-- console, piped through some other client -- gets no ledger row, no advisory
-- lock and no recorded checksum, and it executes one statement at a time in
-- autocommit. That last one is the expensive difference. The ordering below is
-- arranged so that a failure anywhere leaves the schema exactly as it was, and
-- that property comes entirely from the single transaction the runner wraps the
-- whole file in. Statement-at-a-time, a failure partway through leaves issues
-- carrying two nullable columns, a backfill that ran for some rows, and no
-- constraint at all -- which is precisely the interrupted-migration state this
-- ordering exists to make impossible.


CREATE TABLE workspaces (
    id UUID PRIMARY KEY DEFAULT uuidv7(),

    slug TEXT NOT NULL,
    name TEXT NOT NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT workspaces_slug_key UNIQUE (slug),

    -- Lowercase-only, because UNIQUE on plain TEXT is case-sensitive: without
    -- this, 'Vector' and 'vector' are two separate workspaces that both insert
    -- cleanly, and a client varying capitalisation silently addresses a
    -- different tenant while every uniqueness check still reports success.
    -- CITEXT would settle it at the type level and is out of scope here, so
    -- the constraint buys the same guarantee with no extension. The shape also
    -- keeps a slug URL-safe: no leading or trailing hyphen, nothing else.
    CONSTRAINT workspaces_slug_format
        CHECK (slug ~ '^[a-z0-9]([a-z0-9-]*[a-z0-9])?$')
);


CREATE TABLE teams (
    id UUID PRIMARY KEY DEFAULT uuidv7(),

    workspace_id UUID NOT NULL,
    name TEXT NOT NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- RESTRICT on both sides for the same reasons spelled out at
    -- issues_team_fk below; a workspace with live teams is not something to
    -- delete by accident, and relocating a workspace's id is not something to
    -- do silently to its teams.
    CONSTRAINT teams_workspace_fk
        FOREIGN KEY (workspace_id)
        REFERENCES workspaces (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    -- [FK TARGET] Looks redundant beside the primary key on id, and is not: a
    -- foreign key may only reference a UNIQUE-constrained column SET, and this
    -- pair is what issues (workspace_id, team_id) points at. Without it the
    -- composite FK is not merely weaker -- it cannot be declared at all.
    CONSTRAINT teams_workspace_id_key UNIQUE (workspace_id, id)
);


-- The bootstrap tenant, named by literal rather than left to uuidv7().
--
-- Three reasons, in rising order of what they cost to lose:
--
--   * The backfill below has to attach every existing issue to exactly these
--     two rows. Naming them means there is nothing to resolve, and therefore
--     nothing to resolve wrongly -- no subselect that could match a second
--     workspace someone added first, or match none and write NULL.
--   * Local Docker, CI and any Neon branch end up holding identical ids, so a
--     test asserts against a constant instead of querying for the value it is
--     about to check.
--   * ON CONFLICT is banned in this repository, which is what makes a second
--     execution of these two statements fail loudly on the primary key. A
--     generated id would insert happily and leave two bootstrap tenants -- one
--     holding every issue, one holding none, and nothing to say which is
--     which.
--
-- Collision with a generated id is impossible rather than unlikely: the
-- leading 48 bits of a v7 UUID are the Unix-millisecond timestamp uuidv7()
-- stamps at insert time, and all-zero there is 1970. Nothing uuidv7() can
-- produce lands in this range.
INSERT INTO workspaces (id, slug, name)
VALUES ('00000000-0000-7000-8000-000000000001'::UUID, 'vector', 'Vector');

INSERT INTO teams (id, workspace_id, name)
VALUES (
    '00000000-0000-7000-8000-000000000002'::UUID,
    '00000000-0000-7000-8000-000000000001'::UUID,
    'Core'
);


-- Nullable, then backfilled, then constrained -- three steps because the
-- one-step form does not exist. ADD COLUMN ... NOT NULL without a DEFAULT is
-- validated against every existing row as it runs, so it fails outright on a
-- populated issues table, and no backfill can precede it: the column does not
-- exist until that statement finishes. Adding a DEFAULT to dodge that would
-- work and would be worse, because the default outlives the migration and
-- every later insert that forgets a workspace silently lands in the bootstrap
-- tenant instead of being rejected.
ALTER TABLE issues ADD COLUMN workspace_id UUID;
ALTER TABLE issues ADD COLUMN team_id UUID;

-- Deliberately unguarded: no WHERE workspace_id IS NULL.
--
-- The ADD COLUMNs above take ACCESS EXCLUSIVE on issues and the runner holds
-- one transaction across the whole file, so no concurrent insert can appear
-- between them and the constraints below -- every row this statement can see
-- was handed two NULLs two statements ago. A guard would imply some rows might
-- already carry a value, which is not merely unlikely here but unreachable,
-- and a predicate that can never be false is a claim about the schema that
-- is not true.
UPDATE issues
SET workspace_id = '00000000-0000-7000-8000-000000000001'::UUID,
    team_id = '00000000-0000-7000-8000-000000000002'::UUID;

-- What is load-bearing here is that both columns end up NOT NULL, and that the
-- backfill above has already populated them before any integrity check is
-- relied upon. The position of these two statements relative to
-- issues_team_fk is not.
--
-- That distinction was checked against PostgreSQL 18 rather than assumed:
-- building this schema both ways -- SET NOT NULL then ADD CONSTRAINT, and
-- ADD CONSTRAINT then SET NOT NULL -- produces an identical pg_constraint and
-- identical column nullability. With the backfill complete there are no NULLs
-- left for either ordering to disagree about, so the FK validates the same
-- rows either way. The order below is kept because it reads in the order the
-- guarantees are established, not because PostgreSQL requires it.
--
-- The NOT NULLs themselves are permanent, and for a reason that outlives this
-- file. A composite foreign key is MATCH SIMPLE by default, which skips the
-- check entirely for any row where a referencing column is NULL. So making
-- either column nullable again does not relax a constraint -- it quietly
-- switches off tenant-pair enforcement for every row that takes the NULL,
-- while issues_team_fk goes on reading as enforced.
ALTER TABLE issues ALTER COLUMN workspace_id SET NOT NULL;
ALTER TABLE issues ALTER COLUMN team_id SET NOT NULL;

-- The composite FK: the team must live in the issue's own workspace. Two
-- single-column keys would each pass while together describing an issue in
-- workspace A filed against a team in workspace B.
--
-- ON DELETE RESTRICT, emphatically not CASCADE. After the backfill every issue
-- in the database belongs to the one bootstrap team, so a single
-- `DELETE FROM teams WHERE id = ...` under CASCADE destroys 100% of the
-- product's data while the command tag reads `DELETE 1`. ON DELETE SET NULL is
-- not merely undesirable but impossible, since both referencing columns are
-- NOT NULL above. NO ACTION would be equivalent to RESTRICT here and differs
-- only in being deferrable, which nothing in this schema needs.
--
-- ON UPDATE RESTRICT for the mirror-image reason: under ON UPDATE CASCADE a
-- single `UPDATE teams SET workspace_id = ...` relocates every one of that
-- team's issues into another tenant, with no statement anywhere naming issues.
--
-- There is deliberately NO second foreign key from issues.workspace_id to
-- workspaces.id. It would be redundant -- this constraint forces a real teams
-- row, and teams_workspace_fk forces that row's workspace to be real, so the
-- reference is already transitively guaranteed -- and it would not be free: a
-- second FK is a second deletion path into issues, which is a second place to
-- get ON DELETE wrong.
ALTER TABLE issues ADD CONSTRAINT issues_team_fk
    FOREIGN KEY (workspace_id, team_id)
    REFERENCES teams (workspace_id, id)
    ON DELETE RESTRICT ON UPDATE RESTRICT;

-- 001's keyset index is dropped rather than kept alongside the new one. Every
-- product query that this migration exists to enable leads with
-- `workspace_id = $1`, and an index that does not begin with that column can
-- serve such a query only by scanning and filtering at a cost proportional to
-- the GLOBAL row count -- so keeping it would buy write amplification on every
-- insert and update in exchange for a query the application is being changed
-- to stop issuing.
--
-- It leaves a window: until IssueRepository.list orders within a workspace,
-- its `ORDER BY created_at DESC, id DESC` over the whole table has no index
-- behind it. That window is why the repository change belongs in the same
-- phase as this migration and not a later one.
DROP INDEX issues_created_at_id_idx;

-- The same keyset shape as 001's, widened by the tenant key. (id DESC) still
-- breaks created_at ties, which is what keeps the cursor comparison in
-- IssueRepository.list total and therefore stable.
CREATE INDEX issues_workspace_created_at_id_idx
    ON issues (workspace_id, created_at DESC, id DESC);

-- PostgreSQL creates an index on the REFERENCED side of a foreign key and not
-- on the REFERENCING side, so without this every `DELETE FROM teams` and every
-- update of a team's key has to scan issues in full to satisfy the RESTRICT
-- check above. The column order is the FK's column list, in the FK's order,
-- because that is the lookup the referential-integrity check performs.
CREATE INDEX issues_workspace_team_idx ON issues (workspace_id, team_id);
