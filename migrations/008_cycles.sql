-- Cycles: a team's time-boxed iteration, and the column on issues that puts
-- an issue in one.
--
-- Two product rules this file has to make unrepresentable, both of them
-- rules the server is the only place able to hold.
--
-- The first is about ownership: a cycle belongs to exactly one team, and an
-- issue may only join a cycle belonging to ITS OWN team. Two single-column
-- foreign keys --
-- issues.cycle_id -> cycles.id alongside the tenancy pair 002 already
-- declares -- would each pass while together describing an issue on team A
-- sitting in team B's cycle, in another workspace entirely. So the reference
-- carries the tenant pair with it, exactly as migrations/002_tenancy.sql
-- carries the workspace into the team reference, and for the same reason: the
-- server is the only place a rule like this cannot be raced by an application
-- that checked first and wrote second.
--
-- The second is about time: a team's cycles may not overlap. That one is
-- `cycles_no_overlap`, an EXCLUDE over a tstzrange, and it is stated in the
-- schema for exactly the reason the first one is -- a service that SELECTs
-- for an overlap and then INSERTs is two statements with a race between
-- them, and two concurrent creates walk straight through it. See the
-- constraint itself for why the range is half-open and why btree_gist is
-- installed above.
--
-- Apply this ONLY through `python -m scripts.apply_migration
-- migrations/008_cycles.sql`. A hand-run gets no ledger row, no advisory lock
-- and no recorded checksum, and it runs one statement at a time in autocommit
-- -- which here would mean a `cycles` table and a nullable `issues.cycle_id`
-- that no foreign key constrains, a shape in which every cross-team
-- assignment this file exists to refuse would insert cleanly.
--
-- Nothing below backfills or rewrites an existing row. `cycle_id` arrives
-- nullable and stays NULL for every issue already filed; "in no cycle" is a
-- real and permanent state of an issue, not an interim one on the way to a
-- default. There is deliberately no automatic cycle generation here and none
-- planned in this migration's scope: cycles are created explicitly, so no
-- statement in this file invents one.


-- Required by `cycles_no_overlap` below, and by nothing else in this file.
--
-- An EXCLUDE constraint is backed by a GiST index, and stock GiST has no
-- operator class for UUID equality -- so `team_id WITH =` cannot be part of
-- the key without this. The alternative shapes are both worse: "one
-- exclusion per team" is not expressible, and dropping team_id from the key
-- would forbid two DIFFERENT teams from running cycles in the same
-- fortnight, which is the normal state of a workspace rather than an error.
--
-- Guarded, and this is the only statement in the repository allowed to be.
-- Rule 2 of tests/test_migration_lint.py bans defensive DDL because a guard
-- reports success without establishing that the existing object matches the
-- one the migration describes -- but an extension has no shape for the guard
-- to hide. It is present or absent, and neither spelling pins a version, so
-- there is no mismatch for IF NOT EXISTS to mask. `find_if_not_exists`
-- carries the full argument; the exemption is anchored to CREATE EXTENSION
-- alone, and DROP EXTENSION IF EXISTS is still refused.
--
-- Bare would be actively wrong here rather than merely stricter. It fails
-- outright, and permanently, against any database where btree_gist is
-- already installed -- by a platform default, another tool, or an operator
-- installing it out of band -- because the ledger never records a migration
-- that errored. Verified against postgres:18 with the extension present and
-- a role holding no CREATE privilege on the database:
--
--     CREATE EXTENSION btree_gist         -> ERROR: already exists
--     CREATE EXTENSION IF NOT EXISTS ...  -> NOTICE, skipping
--
-- The guarded form short-circuits before the privilege check, which is what
-- lets this run under the unprivileged application role a managed Postgres
-- hands out, on a database where the platform pre-installed the extension.
--
-- btree_gist is trusted on PostgreSQL 13+, so installing it fresh wants the
-- database owner rather than a superuser. It is also transactional -- unlike
-- a value added by `ALTER TYPE`, an extension can be created and USED in the
-- same transaction, which is what lets the constraint below be declared in
-- this same file without splitting it in two.
CREATE EXTENSION IF NOT EXISTS btree_gist;


CREATE TABLE cycles (
    id UUID PRIMARY KEY DEFAULT uuidv7(),

    workspace_id UUID NOT NULL,
    team_id UUID NOT NULL,

    -- The cycle's identity to a human: teams speak of "Cycle 7", and the
    -- name is the optional label on top of it ("Hardening", "Launch week").
    -- INTEGER rather than SMALLINT because the ceiling is a product rule
    -- rather than a storage one, and a team on fortnightly cycles reaches
    -- 32767 in about 1200 years but reaches the end of a SMALLINT column
    -- migration in an afternoon.
    number INTEGER NOT NULL,
    name TEXT,

    starts_at TIMESTAMPTZ NOT NULL,
    ends_at TIMESTAMPTZ NOT NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- No trigger maintains this, deliberately. A touch trigger needs a
    -- PL/pgSQL function body, and tests/test_migration_lint.py records a
    -- known hole (dated 2026-09-02) where such a body's BEGIN reads as
    -- transaction control -- so the first migration to carry one has to fix
    -- that rule first, and that is not this file's job. CycleRepository
    -- writes `updated_at = now()` in every UPDATE it issues; the default
    -- covers the insert.
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- RESTRICT on both sides, for the reasons migrations/002_tenancy.sql
    -- sets out at issues_team_fk. A team with live cycles is not something
    -- to delete by accident, and relocating a team's workspace is not
    -- something to do silently to its cycles. The reference is the composite
    -- pair rather than team_id alone: without workspace_id in it, a cycle
    -- could name a team while claiming a different tenant, and then
    -- issues_cycle_fk below -- which routes an issue's own (workspace_id,
    -- team_id) through this table -- would be validating against a row that
    -- is itself mis-tenanted.
    CONSTRAINT cycles_team_fk
        FOREIGN KEY (workspace_id, team_id)
        REFERENCES teams (workspace_id, id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    -- [FK TARGET] The same shape as teams_workspace_id_key one level up, and
    -- not redundant beside the primary key for the same reason: a foreign
    -- key may only reference a UNIQUE-constrained column SET, and this triple
    -- is what issues (workspace_id, team_id, cycle_id) points at. Without it
    -- issues_cycle_fk cannot be declared at all -- not merely weakened.
    CONSTRAINT cycles_workspace_team_id_key UNIQUE (workspace_id, team_id, id),

    -- Numbering is per team: team A's cycle 7 and team B's cycle 7 are two
    -- different cycles and must both be insertable.
    --
    -- (team_id, number) alone would be exactly equivalent -- cycles_team_fk
    -- forces a row's (workspace_id, team_id) to match a real teams row, and
    -- teams.id is a primary key, so two cycles sharing a team_id necessarily
    -- share a workspace_id. The tenant leads anyway, so that the index this
    -- constraint creates is also the one `CycleRepository.list_for_team`
    -- reads: `workspace_id = $1 AND team_id = $2 ORDER BY number` is served
    -- by its leading columns with no sort at all.
    CONSTRAINT cycles_team_number_key UNIQUE (workspace_id, team_id, number),

    -- A floor, not the product's range. CycleService rejects a number
    -- outside 1..9999 with a structured field error before any connection is
    -- taken; what belongs here is the part that must be true of every row
    -- however it was written -- by a future importer, a backfill, or a
    -- console session -- and "cycle zero" and "cycle minus four" are not
    -- states the product has a meaning for.
    CONSTRAINT cycles_number_positive CHECK (number > 0),

    -- Strictly greater, not >=. A zero-length cycle is not a shorter cycle;
    -- it is a cycle no issue can be worked in, and every "is this the current
    -- cycle" query -- `now() BETWEEN starts_at AND ends_at` and its
    -- half-open variants alike -- disagrees about whether such a row is ever
    -- live. Rejecting it at the source is cheaper than teaching every reader
    -- to cope with it. It is also what keeps `cycles_no_overlap` below
    -- meaningful: an empty tstzrange overlaps nothing, so a zero-length row
    -- would slip past the exclusion constraint entirely.
    CONSTRAINT cycles_dates_ordered CHECK (ends_at > starts_at),

    -- One team runs one cycle at a time: no two of a team's cycles may cover
    -- the same instant.
    --
    -- This is an EXCLUDE and not a service-level check, and the difference is
    -- the whole point of writing it here. `SELECT ... WHERE ranges overlap`
    -- followed by an INSERT is two statements with a gap between them, and
    -- under READ COMMITTED two concurrent creates both read a table in which
    -- the other's row does not exist yet, both find no overlap, and both
    -- insert. Nothing about that is unlikely -- it is one user double-clicking
    -- a button. A GiST exclusion constraint takes the decision inside the
    -- index write, so the second transaction blocks on the first and then
    -- fails, whatever the interleaving.
    --
    -- The rule is enforced rather than left open because "the current cycle"
    -- is a concept the product depends on, and it only has an answer if the
    -- ranges are disjoint: `WHERE now() >= starts_at AND now() < ends_at`
    -- returns at most one row here, and would return an arbitrary number of
    -- them under overlap, leaving every reader to invent its own tiebreak.
    --
    -- '[)' -- half-open, and load-bearing. Cycles are normally written back
    -- to back, so a cycle ending 2026-01-15T00:00Z and the next starting at
    -- that same instant is the ordinary case; under '[]' those two share the
    -- boundary instant and the constraint would refuse the most common thing
    -- a team does. Half-open makes an end exclusive, so they abut and do not
    -- overlap. It is the default for a two-argument tstzrange, and written
    -- out anyway because the whole behaviour of the constraint turns on it.
    --
    -- workspace_id is in the key although team_id alone already implies it --
    -- cycles_team_fk forces a row's team to a real teams row and teams.id is
    -- a primary key, so two cycles sharing a team share a workspace. It is
    -- written out so the constraint reads as tenant-scoped without tracing a
    -- foreign key to prove it, which is the same convention every statement
    -- in app/repositories/ follows.
    CONSTRAINT cycles_no_overlap
        EXCLUDE USING gist (
            workspace_id WITH =,
            team_id WITH =,
            tstzrange(starts_at, ends_at, '[)') WITH &&
        )
);


-- Nullable, and permanently so: an issue in no cycle is the ordinary state of
-- an issue, so there is nothing to backfill and no SET NOT NULL to follow.
--
-- No DEFAULT either, and that is the load-bearing half. A default cycle id
-- would outlive this migration and quietly enrol every future issue in
-- whatever cycle it named -- including issues of other teams, which
-- issues_cycle_fk would then refuse at insert time, turning a defaulted
-- column into a table that rejects ordinary writes.
ALTER TABLE issues ADD COLUMN cycle_id UUID;


-- The constraint this migration exists for: an issue's cycle must belong to
-- the issue's own team, in the issue's own workspace.
--
-- The referencing columns are the issue's own tenancy pair plus the new
-- column, and the referenced columns are the matching triple on cycles. That
-- is what makes a cross-team assignment unrepresentable rather than merely
-- discouraged: to satisfy this, a cycles row must exist whose workspace_id
-- and team_id are the ones already stored on the issue, so there is no value
-- of cycle_id that puts an issue in another team's cycle, and none that puts
-- it in another tenant's. It also constrains the issue in the other
-- direction: moving an issue to another team while it sits in a cycle is
-- refused, because the moved row would no longer match its cycle's team.
--
-- MATCH SIMPLE -- the default, and here it is the mechanism rather than a
-- footnote. Under MATCH SIMPLE a row with ANY NULL among its referencing
-- columns skips the check entirely, and cycle_id is the only one of the
-- three that can be NULL, so the check is skipped exactly when an issue is in
-- no cycle and applies in full whenever it is in one. MATCH FULL would demand
-- all three be NULL together or none be, and since workspace_id and team_id
-- are NOT NULL that would make "no cycle" unrepresentable -- every issue
-- would have to be in one.
--
-- ON DELETE SET NULL (cycle_id) -- and the column list is not decoration.
-- Bare `ON DELETE SET NULL` nulls EVERY referencing column, which here means
-- workspace_id and team_id too: the delete would fail on their NOT NULL
-- constraints, so deleting any cycle that held issues would error with a
-- message about tenancy columns that names nothing about cycles. The listed
-- form (PostgreSQL 15+) nulls only the grouping column, which is the whole
-- intent -- deleting a cycle disbands the grouping and destroys no work.
--
-- That is a deliberate departure from the RESTRICT that 002 argues for on
-- issues_team_fk, and the difference is what the action would destroy. There,
-- the referencing columns are NOT NULL and the only cascade available
-- destroys issues; RESTRICT is the sole safe answer. Here the referenced
-- value is a nullable grouping, so SET NULL discards a cycle membership and
-- nothing else -- and it does it in the same statement, under the same lock,
-- which is what makes it immune to the race an application-side
-- "unassign, then delete" would open between its two statements.
--
-- ON UPDATE RESTRICT, for 002's reason exactly: under ON UPDATE CASCADE a
-- single `UPDATE cycles SET team_id = ...` would drag every one of that
-- cycle's issues into another team's cycle with no statement naming issues.
ALTER TABLE issues ADD CONSTRAINT issues_cycle_fk
    FOREIGN KEY (workspace_id, team_id, cycle_id)
    REFERENCES cycles (workspace_id, team_id, id)
    ON DELETE SET NULL (cycle_id) ON UPDATE RESTRICT;


-- PostgreSQL indexes the REFERENCED side of a foreign key and never the
-- REFERENCING side, so without this every `DELETE FROM cycles` scans issues
-- in full to find the rows whose cycle_id it must null. The columns are the
-- foreign key's own list in the foreign key's own order, because that is the
-- lookup the referential-integrity check performs.
--
-- Partial, and that is what keeps it from duplicating
-- issues_workspace_team_idx from 002. The rows excluded here -- every issue
-- in no cycle, which in a healthy backlog is most of them -- can never
-- satisfy `cycle_id = <some cycle's id>`, so indexing them would buy nothing
-- but size and write amplification. The `cycle_id = $3` the integrity check
-- issues implies `cycle_id IS NOT NULL` (equality is strict), which is what
-- lets the planner prove the predicate and use this index for it.
--
-- 002's issues_workspace_team_idx stays exactly as it is. It is not a prefix
-- of this one in any useful sense: it has to cover the issues this index
-- deliberately omits, because that is the lookup issues_team_fk performs when
-- a team is deleted.
CREATE INDEX issues_workspace_team_cycle_idx
    ON issues (workspace_id, team_id, cycle_id)
    WHERE cycle_id IS NOT NULL;
