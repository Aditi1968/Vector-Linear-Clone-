-- Membership: who belongs to a workspace, in what role, and by what invitation.
--
-- 002 gave every issue a tenant. It gave nobody a reason to be in one, and
-- said so: WorkspaceScope's own docstring records that holding one means a
-- slug resolved, not that the caller may be there. This migration creates the
-- table that answers the second question, and it is the only place that answer
-- is stored -- an authorization decision that reads anything else is reading
-- something a client could have supplied.
--
-- Apply this ONLY through `python -m scripts.apply_migration
-- migrations/004_membership.sql`. A hand-run -- pasted into a console, piped
-- through some other client -- gets no ledger row, no advisory lock and no
-- recorded checksum, and it executes one statement at a time in autocommit.
-- Here that would mean a `workspace_members` that exists while
-- `workspace_invitations` does not, with nothing recording that the file only
-- half-ran.
--
-- DEPENDS ON 003, which creates `users`. `workspace_members_user_fk` below
-- references `users (id)`, so applying this file before 003 fails on that
-- constraint and, inside the single transaction the runner wraps the file in,
-- leaves nothing behind. That failure is the dependency check: the ledger
-- records what has been applied and not what depends on what, so the schema
-- itself is what has to refuse an out-of-order apply.


CREATE TABLE workspace_members (
    workspace_id UUID NOT NULL,
    user_id UUID NOT NULL,

    -- No DEFAULT, unlike the draft in schema_drafts/full_schema.sql. A
    -- default is what an INSERT that forgot the column silently receives, and
    -- here that silence would be a grant: the row still creates a real
    -- membership, at whichever role this file happened to pick. Requiring the
    -- caller to name the role turns the omission into an error instead.
    role TEXT NOT NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- The natural key, and deliberately the whole key: a membership IS the
    -- pair. A surrogate `id` alongside it would be a second identity for a row
    -- that already has one, and it would leave (workspace_id, user_id) merely
    -- unique rather than the thing the row is addressed by -- so a lookup
    -- could be written against either, and two ways to address one row is how
    -- "is this user in this workspace" acquires two answers.
    --
    -- [FK TARGET] It is also the pair a later `issues (workspace_id,
    -- assignee_id)` has to reference, so that the database rather than a
    -- service is what refuses an assignee who is not a member of the issue's
    -- own workspace. A foreign key may only target a UNIQUE-constrained column
    -- set, and a primary key is one, so no separate UNIQUE is needed here --
    -- contrast teams_workspace_id_key in 002, where the pair is not the key.
    CONSTRAINT workspace_members_pkey PRIMARY KEY (workspace_id, user_id),

    -- A CHECK rather than an enum type, and the reason is the runner rather
    -- than taste. Every migration here executes inside one transaction, and
    -- PostgreSQL refuses to *use* an enum value in the same transaction that
    -- added it via ALTER TYPE ... ADD VALUE. So a later migration that adds a
    -- role and backfills existing rows onto it could not be written as a
    -- single file, which is the only shape a file in this directory has.
    -- Widening or narrowing a CHECK is DROP CONSTRAINT then ADD CONSTRAINT,
    -- both ordinary statements inside that same transaction. Dropping an enum
    -- value has no statement at all.
    --
    -- The list reads least- to most-privileged as documentation only. `IN` is
    -- a set membership test and reads no order into its operands; nothing may
    -- infer a privilege ordering from this constraint, because the constraint
    -- does not encode one. What a role permits is the application's decision,
    -- and app.domain.tenancy.WORKSPACE_ROLES is the copy it reads.
    CONSTRAINT workspace_members_role_check
        CHECK (role IN ('member', 'admin', 'owner')),

    -- RESTRICT on both sides, matching 002's stance on teams and issues.
    -- ON DELETE CASCADE from workspaces would drop every grant into a
    -- workspace with no statement anywhere naming workspace_members -- and
    -- once `issues (workspace_id, assignee_id)` references this table with
    -- ON DELETE SET NULL, that cascade silently unassigns work as a
    -- third-order effect of a one-line delete against a third table.
    CONSTRAINT workspace_members_workspace_fk
        FOREIGN KEY (workspace_id)
        REFERENCES workspaces (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    -- Same reasoning from the users side. Removing an account that still
    -- holds memberships is a thing to do deliberately, membership by
    -- membership, rather than something that happens on the way to deleting a
    -- row in another table.
    CONSTRAINT workspace_members_user_fk
        FOREIGN KEY (user_id)
        REFERENCES users (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT
);


-- PostgreSQL indexes the REFERENCED side of a foreign key and not the
-- REFERENCING side, so without this every deletion from `users` scans
-- workspace_members in full to satisfy the RESTRICT check above. It is also
-- the index behind "which workspaces is this user in", which is the query a
-- workspace switcher runs on every page load.
--
-- `user_id` alone, not (user_id, workspace_id): the primary key already covers
-- the other direction, and the listing query reads `role` and `created_at` off
-- the heap either way, so a second key column would buy write amplification
-- and no scan it can serve that this one cannot.
CREATE INDEX workspace_members_user_idx ON workspace_members (user_id);


CREATE TABLE workspace_invitations (
    id UUID PRIMARY KEY DEFAULT uuidv7(),

    workspace_id UUID NOT NULL,

    -- The address the invitation was sent to, stored as the inviter wrote it.
    -- Neither normalised nor unique, because it is not a key here: redemption
    -- is by token below, so nothing looks a row up by email. Matching an
    -- accepted invitation to an account is `users.email`'s job, and 003 owns
    -- how that column compares.
    email TEXT NOT NULL,

    role TEXT NOT NULL,

    -- Only the hash is stored. An invitation token is a bearer credential:
    -- whoever holds it can join the workspace, so keeping the token itself
    -- would turn any read of this table -- a backup, a support query, a
    -- `SELECT *` in a log -- into a grant of access to every tenant with an
    -- open invitation.
    token_hash TEXT NOT NULL,

    -- No default, deliberately. An invitation with no expiry is a permanent
    -- credential, and a schema-supplied lifetime would be a policy decision
    -- made by whoever wrote this file rather than by the code issuing the
    -- invitation.
    expires_at TIMESTAMPTZ NOT NULL,

    -- NULL until accepted. The timestamp is what "accepted" means, so there is
    -- no second boolean that can come to disagree with it.
    accepted_at TIMESTAMPTZ,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT workspace_invitations_workspace_fk
        FOREIGN KEY (workspace_id)
        REFERENCES workspaces (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    -- The same role vocabulary as workspace_members, for the plain reason that
    -- accepting an invitation writes this value into that column: a role
    -- acceptable here and not there is an invitation nobody can accept. Two
    -- CHECKs rather than one shared DOMAIN, so both constraints are readable
    -- in one file; tests/test_membership_scope.py is what pins them equal to
    -- each other and to WORKSPACE_ROLES.
    CONSTRAINT workspace_invitations_role_check
        CHECK (role IN ('member', 'admin', 'owner')),

    -- The redemption key: a caller presents a token, the server hashes it and
    -- finds this row. UNIQUE because two rows sharing a hash would make that
    -- lookup ambiguous, and an ambiguous credential lookup resolves to
    -- whichever workspace the planner reached first.
    CONSTRAINT workspace_invitations_token_hash_key UNIQUE (token_hash),

    -- A lowercase-hex sha256 digest. This does NOT prove the column holds a
    -- hash: a raw token that happened to be 64 hex characters would satisfy
    -- it. It is a shape check and is worth stating as one -- it rejects the
    -- plausible mistakes (an empty string, a base64 token, an email address, a
    -- truncated digest) and leaves the actual guarantee where it belongs, in
    -- the code that writes the column.
    CONSTRAINT workspace_invitations_token_hash_format
        CHECK (token_hash ~ '^[a-f0-9]{64}$'),

    -- An invitation that expired before it was created cannot be accepted by
    -- anyone, so it is a meaningless row rather than a short-lived one.
    CONSTRAINT workspace_invitations_expiry_after_creation
        CHECK (expires_at > created_at)
);


-- Deliberately no unique key over (workspace_id, email). Two invitations to
-- one address are ordinary -- the first expired, the second re-sent -- and a
-- partial unique index over the unaccepted ones would make re-inviting
-- impossible without first removing a row, which is not a shape the files in
-- this directory take. Redemption is by token, so a duplicate is at worst two
-- ways in for the same person and never an ambiguous lookup.
--
-- This index exists for the referencing side of
-- workspace_invitations_workspace_fk, for the same reason as
-- workspace_members_user_idx above: without it every deletion from
-- `workspaces` scans this table in full to satisfy RESTRICT. It also serves
-- "the invitations for this workspace", which is the only way the product
-- lists them.
CREATE INDEX workspace_invitations_workspace_idx
    ON workspace_invitations (workspace_id);


-- 004 seeds nothing, and the contrast with 002 is deliberate rather than an
-- omission. 002 could name a bootstrap workspace and team as literals because
-- a workspace is a container: creating one grants nobody anything. A
-- workspace_members row is the opposite -- it *is* a grant -- and this file
-- does not create the `users` it would have to name. A seeded membership would
-- therefore either point at a user id invented here, which no login can ever
-- reach, or at a real account, which would hand that account a real tenant as
-- a side effect of applying a migration. Memberships are created by the
-- application, under authorization, or not at all.
