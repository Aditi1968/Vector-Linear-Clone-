-- ============================================================================
-- Vector — 002_full_schema.sql
-- Target: PostgreSQL 18 (Neon)
--
-- A Linear-style issue tracker. Multi-tenant: a workspace IS the tenant.
--
-- DESIGN PRINCIPLE
-- ----------------
-- Tenant isolation is enforced by the DATABASE, not by application query
-- filters. Every cross-entity reference carries the tenant key (workspace_id,
-- or team_id where the scope is narrower) and is validated by a COMPOSITE
-- foreign key. A single-column `REFERENCES workflow_states(id)` is insufficient
-- because it happily permits an issue in team ENG to point at a state owned by
-- team MARKETING. Composite FKs make that state structurally unrepresentable.
--
-- To make composite FKs declarable, workspace_id is denormalised onto issues,
-- comments, issue_labels and issue_relations. That denormalisation is not a
-- convenience — it is the mechanism.
--
-- CONVENTIONS
-- -----------
--   * Primary keys are UUID DEFAULT uuidv7() (built into PostgreSQL 18).
--     Append-only event/log tables use BIGSERIAL instead, because monotonic
--     integers are cheaper to scan, claim and checkpoint.
--   * Every timestamp is TIMESTAMPTZ. Never TIMESTAMP.
--   * Nullability is decided per column, deliberately, and stated explicitly.
--   * Composite FKs are left at the default MATCH SIMPLE (see the note on
--     issues.assignee_id below).
-- ============================================================================

BEGIN;

-- ----------------------------------------------------------------------------
-- Extensions
-- ----------------------------------------------------------------------------
-- citext: case-insensitive text, used for users.email so that
-- 'Ada@Example.com' and 'ada@example.com' collide on the UNIQUE index rather
-- than creating two accounts.
CREATE EXTENSION IF NOT EXISTS citext;

-- NOTE: uuidv7() requires no extension on PostgreSQL 18. It is time-ordered,
-- so UUID primary keys stay index-local on insert instead of scattering writes
-- across the B-tree the way uuidv4() does.


-- ============================================================================
-- SECTION 1 — IDENTITY
-- Users exist globally, above any tenant. Membership in a workspace is what
-- grants scope; see workspace_members.
-- ============================================================================

CREATE TABLE users (
    id              UUID        PRIMARY KEY DEFAULT uuidv7(),
    email           CITEXT      NOT NULL UNIQUE,
    name            TEXT        NOT NULL,
    avatar_url      TEXT        NULL,
    timezone        TEXT        NOT NULL DEFAULT 'UTC',
    is_active       BOOLEAN     NOT NULL DEFAULT TRUE,
    last_seen_at    TIMESTAMPTZ NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at      TIMESTAMPTZ NULL
);

COMMENT ON COLUMN users.email IS
    'CITEXT: uniqueness is case-insensitive, preventing duplicate accounts that '
    'differ only in capitalisation.';


CREATE TABLE auth_identities (
    id                UUID        PRIMARY KEY DEFAULT uuidv7(),
    user_id           UUID        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider          TEXT        NOT NULL,   -- 'google' | 'github' | 'saml' | 'password'
    provider_subject  TEXT        NOT NULL,   -- the provider's stable subject ('sub') claim
    email_at_provider CITEXT      NULL,       -- snapshot; may drift from users.email
    last_login_at     TIMESTAMPTZ NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- INVARIANT: one identity per (provider, subject). Prevents two Vector users
    -- from both claiming the same upstream Google/GitHub account, which would
    -- make SSO login ambiguous.
    CONSTRAINT auth_identities_provider_subject_key
        UNIQUE (provider, provider_subject)
);

CREATE INDEX auth_identities_user_id_idx ON auth_identities (user_id);


CREATE TABLE sessions (
    id           UUID        PRIMARY KEY DEFAULT uuidv7(),
    user_id      UUID        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    -- Only the hash is stored. The bearer token itself is never persisted, so a
    -- database disclosure does not yield usable session credentials.
    token_hash   BYTEA       NOT NULL UNIQUE,
    ip           INET        NULL,            -- NULL when the origin IP is unknown/proxied away
    user_agent   TEXT        NULL,
    expires_at   TIMESTAMPTZ NOT NULL,
    revoked_at   TIMESTAMPTZ NULL,            -- NULL == live session
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at TIMESTAMPTZ NULL
);

-- Hot path: "list/validate this user's live sessions". Partial, because revoked
-- rows are retained for audit but never queried on the auth path.
CREATE INDEX sessions_user_id_active_idx
    ON sessions (user_id)
    WHERE revoked_at IS NULL;


-- ============================================================================
-- SECTION 2 — TENANCY
-- workspace is the tenant root. Everything below cascades from it.
-- ============================================================================

CREATE TABLE workspaces (
    id          UUID        PRIMARY KEY DEFAULT uuidv7(),
    name        TEXT        NOT NULL,
    slug        TEXT        NOT NULL UNIQUE,
    -- Per-workspace realtime cursor. Clients hold a watermark and ask for
    -- everything above it; see sync_events.publication_seq.
    sync_seq    BIGINT      NOT NULL DEFAULT 0,
    settings    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    archived_at TIMESTAMPTZ NULL
);

COMMENT ON COLUMN workspaces.sync_seq IS
    'Monotonic per-workspace publication counter. Allocated (not read) when '
    'emitting sync_events, so every tenant has its own gap-free stream.';


CREATE TABLE workspace_members (
    id           UUID        PRIMARY KEY DEFAULT uuidv7(),
    workspace_id UUID        NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id      UUID        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role         TEXT        NOT NULL DEFAULT 'member',  -- 'owner' | 'admin' | 'member' | 'guest'
    invited_by   UUID        NULL REFERENCES users(id) ON DELETE SET NULL,
    joined_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- [FK TARGET] Serves double duty:
    --   1. INVARIANT: a user joins a workspace at most once.
    --   2. It is the target of issues (workspace_id, assignee_id). Without this
    --      UNIQUE, that composite FK cannot be declared at all.
    CONSTRAINT workspace_members_workspace_user_key
        UNIQUE (workspace_id, user_id)
);

CREATE INDEX workspace_members_user_id_idx ON workspace_members (user_id);


CREATE TABLE teams (
    id             UUID        PRIMARY KEY DEFAULT uuidv7(),
    workspace_id   UUID        NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    name           TEXT        NOT NULL,
    key            TEXT        NOT NULL,   -- 'ENG', 'MKT' — the identifier prefix
    description    TEXT        NULL,
    -- Drives the ENG-123 sequence. Bumped under row lock when an issue is
    -- created; a plain SEQUENCE would be gap-prone and not per-team.
    issue_counter  INT         NOT NULL DEFAULT 0,
    is_private     BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    archived_at    TIMESTAMPTZ NULL,

    -- INVARIANT: team keys are unique per tenant, so 'ENG-123' resolves to
    -- exactly one issue within a workspace.
    CONSTRAINT teams_workspace_key_key UNIQUE (workspace_id, key),

    -- [FK TARGET] Looks redundant next to the PK on id, and is not: a foreign
    -- key may only target a UNIQUE-constrained column SET. This pair is what
    -- issues (workspace_id, team_id) points at, which is how the database
    -- proves a team belongs to the same workspace as the issue.
    CONSTRAINT teams_workspace_id_key UNIQUE (workspace_id, id)
);


CREATE TABLE team_members (
    id         UUID        PRIMARY KEY DEFAULT uuidv7(),
    team_id    UUID        NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    user_id    UUID        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role       TEXT        NOT NULL DEFAULT 'member',  -- 'lead' | 'member'
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- INVARIANT: a user joins a team at most once.
    CONSTRAINT team_members_team_user_key UNIQUE (team_id, user_id)
);

CREATE INDEX team_members_user_id_idx ON team_members (user_id);


-- ============================================================================
-- SECTION 3 — DOMAIN
-- ============================================================================

CREATE TABLE workflow_states (
    id          UUID        PRIMARY KEY DEFAULT uuidv7(),
    team_id     UUID        NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    name        TEXT        NOT NULL,
    -- Category the UI groups by: 'backlog'|'unstarted'|'started'|'completed'|'canceled'
    type        TEXT        NOT NULL,
    color       TEXT        NOT NULL DEFAULT '#95a2b3',
    position    NUMERIC     NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- INVARIANT: state names are unique within a team.
    CONSTRAINT workflow_states_team_name_key UNIQUE (team_id, name),

    -- [FK TARGET] Redundant-looking, not redundant — an FK can only reference a
    -- UNIQUE column set. Target of issues (team_id, state_id): the pair is what
    -- forbids an ENG issue from sitting in a MARKETING workflow state.
    CONSTRAINT workflow_states_team_id_key UNIQUE (team_id, id)
);


CREATE TABLE projects (
    id           UUID        PRIMARY KEY DEFAULT uuidv7(),
    workspace_id UUID        NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    name         TEXT        NOT NULL,
    description  TEXT        NULL,
    status       TEXT        NOT NULL DEFAULT 'planned',  -- 'planned'|'started'|'paused'|'completed'|'canceled'
    lead_id      UUID        NULL REFERENCES users(id) ON DELETE SET NULL,
    start_date   DATE        NULL,
    target_date  DATE        NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    archived_at  TIMESTAMPTZ NULL,

    -- [FK TARGET] Target of issues (workspace_id, project_id). Proves a project
    -- and the issues filed into it share a tenant.
    CONSTRAINT projects_workspace_id_key UNIQUE (workspace_id, id)
);

CREATE INDEX projects_workspace_id_idx ON projects (workspace_id);


CREATE TABLE project_milestones (
    id           UUID        PRIMARY KEY DEFAULT uuidv7(),
    workspace_id UUID        NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    project_id   UUID        NOT NULL,
    name         TEXT        NOT NULL,
    description  TEXT        NULL,
    target_date  DATE        NULL,
    sort_order   TEXT        NOT NULL,   -- fractional index, see issues.sort_order
    completed_at TIMESTAMPTZ NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- COMPOSITE FK: a milestone cannot hang off a project in another workspace.
    CONSTRAINT project_milestones_project_fk
        FOREIGN KEY (workspace_id, project_id)
        REFERENCES projects (workspace_id, id) ON DELETE CASCADE
);

CREATE INDEX project_milestones_project_id_idx ON project_milestones (project_id);


CREATE TABLE cycles (
    id                     UUID        PRIMARY KEY DEFAULT uuidv7(),
    team_id                UUID        NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    number                 INT         NOT NULL,   -- 'Cycle 14'
    name                   TEXT        NULL,
    starts_at              TIMESTAMPTZ NOT NULL,
    ends_at                TIMESTAMPTZ NOT NULL,
    completed_at           TIMESTAMPTZ NULL,
    -- Idempotency guard for the end-of-cycle job that sweeps unfinished issues
    -- into the next cycle. Set once; a re-run that finds it non-NULL is a no-op,
    -- so a retried or duplicated job cannot roll issues over twice.
    rollover_completed_at  TIMESTAMPTZ NULL,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT cycles_dates_check CHECK (ends_at > starts_at),

    -- INVARIANT: cycle numbers are a per-team sequence.
    CONSTRAINT cycles_team_number_key UNIQUE (team_id, number),

    -- [FK TARGET] Target of issues (team_id, cycle_id): an issue may only be
    -- scheduled into a cycle owned by its own team.
    CONSTRAINT cycles_team_id_key UNIQUE (team_id, id)
);


CREATE TABLE labels (
    id           UUID        PRIMARY KEY DEFAULT uuidv7(),
    workspace_id UUID        NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    name         TEXT        NOT NULL,
    color        TEXT        NOT NULL DEFAULT '#6b7280',
    description  TEXT        NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- INVARIANT: label names are unique per tenant.
    CONSTRAINT labels_workspace_name_key UNIQUE (workspace_id, name),

    -- [FK TARGET] Target of issue_labels (workspace_id, label_id).
    CONSTRAINT labels_workspace_id_key UNIQUE (workspace_id, id)
);


-- ----------------------------------------------------------------------------
-- issues — the centre of the schema, and where tenant isolation is enforced
-- most aggressively. workspace_id is denormalised here (it is derivable via
-- team_id) precisely so that the project/label/assignee composite FKs below
-- can be declared.
-- ----------------------------------------------------------------------------
CREATE TABLE issues (
    id           UUID        PRIMARY KEY DEFAULT uuidv7(),
    workspace_id UUID        NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    team_id      UUID        NOT NULL,
    number       INT         NOT NULL,   -- 123 in ENG-123, from teams.issue_counter
    identifier   TEXT        NOT NULL,   -- 'ENG-123', denormalised for direct lookup/display
    title        TEXT        NOT NULL,
    description  TEXT        NULL,
    state_id     UUID        NOT NULL,
    priority     SMALLINT    NOT NULL DEFAULT 0,  -- 0 none, 1 urgent, 2 high, 3 normal, 4 low
    estimate     NUMERIC     NULL,
    -- Fractional index (e.g. 'a0', 'a0V', 'a1'). TEXT, not INT, so an issue can
    -- be dropped between two neighbours by minting a key between their strings
    -- — a single-row UPDATE instead of renumbering the whole column.
    sort_order   TEXT        NOT NULL,
    assignee_id  UUID        NULL,
    creator_id   UUID        NOT NULL,
    project_id   UUID        NULL,
    cycle_id     UUID        NULL,
    parent_id    UUID        NULL,
    -- Optimistic concurrency token. Writers UPDATE ... WHERE version = $n and
    -- treat 0 rows affected as a conflict; also published on sync_events so
    -- clients can discard stale deltas.
    version      INT         NOT NULL DEFAULT 1,
    due_date     DATE        NULL,
    started_at   TIMESTAMPTZ NULL,
    completed_at TIMESTAMPTZ NULL,
    canceled_at  TIMESTAMPTZ NULL,
    archived_at  TIMESTAMPTZ NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT issues_priority_check CHECK (priority BETWEEN 0 AND 4),

    -- INVARIANT: ENG-123 identifies exactly one issue.
    CONSTRAINT issues_team_number_key UNIQUE (team_id, number),

    -- INVARIANT: the denormalised identifier is unique per tenant, so it cannot
    -- silently drift out of sync with the (team_id, number) it is derived from.
    -- Without this, a bad write could stamp two issues 'ENG-123' while their
    -- underlying numbers stayed distinct, and lookups by identifier would be
    -- ambiguous while (team_id, number) still looked healthy.
    CONSTRAINT issues_workspace_identifier_key UNIQUE (workspace_id, identifier),

    -- [FK TARGETS] Both look redundant against the PK on id. Neither is: a
    -- foreign key can only target a uniquely-constrained column SET, so these
    -- exist to be pointed at.
    --   (team_id, id)      <- issues.parent_id, issue_relations.*
    --   (workspace_id, id) <- issue_labels.issue_id, comments.issue_id
    CONSTRAINT issues_team_id_key      UNIQUE (team_id, id),
    CONSTRAINT issues_workspace_id_key UNIQUE (workspace_id, id),

    -- COMPOSITE FK: the team must live in the issue's workspace. Blocks an
    -- issue that claims workspace A while pointing at a team owned by B.
    CONSTRAINT issues_team_fk
        FOREIGN KEY (workspace_id, team_id)
        REFERENCES teams (workspace_id, id) ON DELETE CASCADE,

    -- COMPOSITE FK: the workflow state must belong to the issue's own team.
    -- This is the case a plain REFERENCES workflow_states(id) would let through:
    -- an ENG issue sitting in a MARKETING column.
    --
    -- DEFERRABLE INITIALLY DEFERRED is load-bearing, not decoration. Two cascade
    -- paths converge on issues:
    --
    --   DELETE workspaces
    --     └─> teams (CASCADE)
    --           ├─> workflow_states (CASCADE)   fires first
    --           └─> issues          (CASCADE, via issues_team_fk)
    --
    -- RI triggers fire in internal trigger-name order, which is OID-derived, so
    -- workflow_states (declared earlier in this file) cascades before issues.
    -- An undeferred check therefore runs at a moment when the workflow_states
    -- rows are ALREADY GONE but the issues referencing them are NOT YET deleted,
    -- and raises a spurious violation — killing every DELETE FROM workspaces and
    -- DELETE FROM teams. Deferring to COMMIT lets both cascades finish first, by
    -- which point there is nothing left to violate.
    --
    -- NO ACTION rather than RESTRICT because RESTRICT cannot be deferred at all;
    -- deferrability is the entire difference between the two. The guarantee is
    -- unchanged — a workflow state with live issues still cannot be deleted, the
    -- error simply surfaces at COMMIT instead of at the statement.
    CONSTRAINT issues_state_fk
        FOREIGN KEY (team_id, state_id)
        REFERENCES workflow_states (team_id, id)
        ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,

    -- COMPOSITE FK: an issue may only be scheduled into its own team's cycle.
    -- ON DELETE SET NULL (cycle_id) nulls only the cycle column — a bare SET
    -- NULL would try to null team_id too, which is NOT NULL.
    CONSTRAINT issues_cycle_fk
        FOREIGN KEY (team_id, cycle_id)
        REFERENCES cycles (team_id, id) ON DELETE SET NULL (cycle_id),

    -- COMPOSITE FK (self): sub-issues stay inside one team, so a hierarchy can
    -- never straddle teams. Deleting a parent promotes children to top level.
    CONSTRAINT issues_parent_fk
        FOREIGN KEY (team_id, parent_id)
        REFERENCES issues (team_id, id) ON DELETE SET NULL (parent_id),

    -- COMPOSITE FK: the project must be in the same workspace as the issue.
    CONSTRAINT issues_project_fk
        FOREIGN KEY (workspace_id, project_id)
        REFERENCES projects (workspace_id, id) ON DELETE SET NULL (project_id),

    -- COMPOSITE FK — the important one. Note the target: workspace_members, NOT
    -- users. The database therefore guarantees the assignee is a MEMBER of this
    -- workspace, not merely a user who exists somewhere in the system. A plain
    -- REFERENCES users(id) would let any account in the database be assigned
    -- work inside a tenant they have no membership in.
    --
    -- MATCH SIMPLE (the default, deliberately not MATCH FULL): when any column
    -- of the referencing set is NULL the constraint is SKIPPED entirely. Here
    -- assignee_id IS NULL means the row is never checked — which is exactly the
    -- behaviour we want, because it is what permits unassigned issues. Under
    -- MATCH FULL a NULL assignee_id would be rejected outright (all-or-nothing),
    -- so every issue would need an assignee. Do not "fix" this to MATCH FULL.
    --
    -- ON DELETE SET NULL (assignee_id): removing someone from a workspace
    -- unassigns their issues rather than deleting the issues.
    CONSTRAINT issues_assignee_fk
        FOREIGN KEY (workspace_id, assignee_id)
        REFERENCES workspace_members (workspace_id, user_id)
        ON DELETE SET NULL (assignee_id),

    -- Deliberately NOT a cascade, and deliberately NOT composite. Deleting a
    -- user must never delete the issues they filed — the work outlives the
    -- employee. RESTRICT forces the caller to reassign authorship or soft-delete
    -- the user (users.deleted_at) instead. Not tenant-scoped because a creator
    -- may since have left the workspace; the record is historical.
    CONSTRAINT issues_creator_fk
        FOREIGN KEY (creator_id) REFERENCES users (id) ON DELETE RESTRICT
);

-- Primary board/list query: a team's issues newest-first, excluding archived.
-- (id DESC) breaks created_at ties so keyset pagination is stable.
CREATE INDEX issues_team_created_idx
    ON issues (team_id, created_at DESC, id DESC)
    WHERE archived_at IS NULL;

-- Kanban column render: issues of one state, in fractional-index order.
CREATE INDEX issues_team_state_sort_idx
    ON issues (team_id, state_id, sort_order);

-- "My issues" across teams; archived rows are excluded from the index entirely.
CREATE INDEX issues_assignee_idx
    ON issues (assignee_id)
    WHERE archived_at IS NULL;

CREATE INDEX issues_project_id_idx ON issues (project_id) WHERE project_id IS NOT NULL;
CREATE INDEX issues_cycle_id_idx   ON issues (cycle_id)   WHERE cycle_id IS NOT NULL;
CREATE INDEX issues_parent_id_idx  ON issues (parent_id)  WHERE parent_id IS NOT NULL;


CREATE TABLE issue_labels (
    id           UUID        PRIMARY KEY DEFAULT uuidv7(),
    -- Denormalised solely to make both composite FKs below declarable.
    workspace_id UUID        NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    issue_id     UUID        NOT NULL,
    label_id     UUID        NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- INVARIANT: a label is applied to an issue at most once.
    CONSTRAINT issue_labels_issue_label_key UNIQUE (issue_id, label_id),

    -- COMPOSITE FKs: because BOTH sides are pinned to the same workspace_id
    -- column, a row cannot pair an issue from workspace A with a label from
    -- workspace B — the single workspace_id value must satisfy both parents.
    CONSTRAINT issue_labels_issue_fk
        FOREIGN KEY (workspace_id, issue_id)
        REFERENCES issues (workspace_id, id) ON DELETE CASCADE,
    CONSTRAINT issue_labels_label_fk
        FOREIGN KEY (workspace_id, label_id)
        REFERENCES labels (workspace_id, id) ON DELETE CASCADE
);

CREATE INDEX issue_labels_label_id_idx ON issue_labels (label_id);


CREATE TABLE issue_relations (
    id               UUID        PRIMARY KEY DEFAULT uuidv7(),
    workspace_id     UUID        NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    -- team_id is the shared key for both endpoints below: relations are
    -- team-local by construction.
    team_id          UUID        NOT NULL,
    issue_id         UUID        NOT NULL,
    related_issue_id UUID        NOT NULL,
    type             TEXT        NOT NULL,  -- 'blocks'|'blocked_by'|'relates'|'duplicates'
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- INVARIANT: an issue cannot block/duplicate/relate to itself.
    CONSTRAINT issue_relations_self_check CHECK (issue_id <> related_issue_id),

    -- INVARIANT: one relation of a given type per ordered pair.
    CONSTRAINT issue_relations_pair_key UNIQUE (issue_id, related_issue_id, type),

    -- COMPOSITE FKs: both endpoints resolve against the SAME team_id, so a
    -- relation can never link an ENG issue to a MARKETING issue.
    CONSTRAINT issue_relations_issue_fk
        FOREIGN KEY (team_id, issue_id)
        REFERENCES issues (team_id, id) ON DELETE CASCADE,
    CONSTRAINT issue_relations_related_issue_fk
        FOREIGN KEY (team_id, related_issue_id)
        REFERENCES issues (team_id, id) ON DELETE CASCADE
);

CREATE INDEX issue_relations_related_issue_id_idx ON issue_relations (related_issue_id);


CREATE TABLE comments (
    id                UUID        PRIMARY KEY DEFAULT uuidv7(),
    workspace_id      UUID        NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    issue_id          UUID        NOT NULL,
    author_id         UUID        NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    parent_comment_id UUID        NULL REFERENCES comments(id) ON DELETE CASCADE,
    body              TEXT        NOT NULL,
    edited_at         TIMESTAMPTZ NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at        TIMESTAMPTZ NULL,

    -- COMPOSITE FK: a comment cannot be attached to an issue in a different
    -- workspace. Cascade: deleting an issue deletes its discussion.
    CONSTRAINT comments_issue_fk
        FOREIGN KEY (workspace_id, issue_id)
        REFERENCES issues (workspace_id, id) ON DELETE CASCADE
);

-- Comment thread render: oldest-first within one issue.
CREATE INDEX comments_issue_created_idx ON comments (issue_id, created_at);


CREATE TABLE reactions (
    id         UUID        PRIMARY KEY DEFAULT uuidv7(),
    comment_id UUID        NOT NULL REFERENCES comments(id) ON DELETE CASCADE,
    user_id    UUID        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    emoji      TEXT        NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- INVARIANT: one user contributes at most one of each emoji per comment,
    -- so a double-tap cannot inflate the count.
    CONSTRAINT reactions_comment_user_emoji_key UNIQUE (comment_id, user_id, emoji)
);


CREATE TABLE notifications (
    id           UUID        PRIMARY KEY DEFAULT uuidv7(),
    workspace_id UUID        NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id      UUID        NOT NULL REFERENCES users(id) ON DELETE CASCADE,  -- recipient
    actor_id     UUID        NULL REFERENCES users(id) ON DELETE SET NULL,     -- NULL for system events
    type         TEXT        NOT NULL,  -- 'assigned'|'mentioned'|'commented'|'state_changed'|...
    issue_id     UUID        NULL,
    -- Single-column by design: a comment is already tenant-pinned through
    -- comments_issue_fk, so reaching it via comment_id cannot cross a tenant
    -- boundary that the issue-side FK below does not already close.
    comment_id   UUID        NULL REFERENCES comments(id) ON DELETE CASCADE,
    metadata     JSONB       NOT NULL DEFAULT '{}'::jsonb,
    read_at      TIMESTAMPTZ NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- COMPOSITE FK: a notification cannot point at an issue in another tenant.
    -- Matches the pattern used for comments, issue_labels and integration_links.
    -- MATCH SIMPLE: issue_id IS NULL skips the check, which is what permits
    -- notifications not attached to any issue (workspace invites, digests).
    CONSTRAINT notifications_issue_fk
        FOREIGN KEY (workspace_id, issue_id)
        REFERENCES issues (workspace_id, id) ON DELETE CASCADE
);

-- Unread inbox badge + list. Partial: the read backlog grows without bound but
-- is only ever paged through, never counted on the hot path.
CREATE INDEX notifications_user_unread_idx
    ON notifications (user_id, created_at DESC)
    WHERE read_at IS NULL;


-- ============================================================================
-- SECTION 4 — API SURFACE
-- ============================================================================

CREATE TABLE api_keys (
    id           UUID        PRIMARY KEY DEFAULT uuidv7(),
    workspace_id UUID        NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    created_by   UUID        NULL REFERENCES users(id) ON DELETE SET NULL,
    name         TEXT        NOT NULL,
    -- Lookup/secret split: key_id is the public, indexed handle presented with
    -- every request; key_hash is the verifier. Authentication is one indexed
    -- equality on key_id followed by a constant-time hash compare, so the
    -- secret itself is never used as a search key and never stored in clear.
    key_id       TEXT        NOT NULL UNIQUE,
    key_hash     BYTEA       NOT NULL,
    key_prefix   TEXT        NOT NULL,   -- 'vct_live_a1b2' — display only, for key pickers
    scopes       TEXT[]      NOT NULL DEFAULT '{}',
    last_used_at TIMESTAMPTZ NULL,
    expires_at   TIMESTAMPTZ NULL,       -- NULL == never expires
    revoked_at   TIMESTAMPTZ NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX api_keys_workspace_active_idx
    ON api_keys (workspace_id)
    WHERE revoked_at IS NULL;


CREATE TABLE webhooks (
    id            UUID        PRIMARY KEY DEFAULT uuidv7(),
    workspace_id  UUID        NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    url           TEXT        NOT NULL,
    secret_hash   BYTEA       NOT NULL,   -- HMAC signing secret, stored hashed
    event_types   TEXT[]      NOT NULL DEFAULT '{}',
    is_enabled    BOOLEAN     NOT NULL DEFAULT TRUE,
    failure_count INT         NOT NULL DEFAULT 0,  -- drives auto-disable after N failures
    created_by    UUID        NULL REFERENCES users(id) ON DELETE SET NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX webhooks_workspace_enabled_idx
    ON webhooks (workspace_id)
    WHERE is_enabled;


CREATE TABLE webhook_deliveries (
    -- BIGSERIAL: append-only, high-volume, scanned by range. No UUID needed.
    id              BIGSERIAL   PRIMARY KEY,
    webhook_id      UUID        NOT NULL REFERENCES webhooks(id) ON DELETE CASCADE,
    workspace_id    UUID        NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    event_type      TEXT        NOT NULL,
    payload         JSONB       NOT NULL,
    status          TEXT        NOT NULL DEFAULT 'pending',  -- 'pending'|'delivered'|'failed'
    attempt         INT         NOT NULL DEFAULT 0,
    response_status INT         NULL,   -- NULL until a response is received
    response_body   TEXT        NULL,
    error           TEXT        NULL,
    next_retry_at   TIMESTAMPTZ NULL,
    delivered_at    TIMESTAMPTZ NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX webhook_deliveries_webhook_created_idx
    ON webhook_deliveries (webhook_id, created_at DESC);

-- Retry scanner: only rows still awaiting delivery.
CREATE INDEX webhook_deliveries_pending_idx
    ON webhook_deliveries (next_retry_at)
    WHERE status <> 'delivered';


CREATE TABLE integrations (
    id                     UUID        PRIMARY KEY DEFAULT uuidv7(),
    workspace_id           UUID        NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    provider               TEXT        NOT NULL,   -- 'github'|'slack'|'figma'|'sentry'
    external_account_id    TEXT        NULL,
    -- Ciphertext, not text: the OAuth token is encrypted by the application
    -- before it reaches the database. key_version names the KEK/DEK generation
    -- used, so keys can be rotated and old rows decrypted lazily rather than
    -- requiring a big-bang re-encrypt.
    access_token_encrypted BYTEA       NULL,
    refresh_token_encrypted BYTEA      NULL,
    key_version            INT         NOT NULL DEFAULT 1,
    token_expires_at       TIMESTAMPTZ NULL,
    config                 JSONB       NOT NULL DEFAULT '{}'::jsonb,
    is_enabled             BOOLEAN     NOT NULL DEFAULT TRUE,
    installed_by           UUID        NULL REFERENCES users(id) ON DELETE SET NULL,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- INVARIANT: one installation per provider per tenant.
    CONSTRAINT integrations_workspace_provider_key UNIQUE (workspace_id, provider)
);


CREATE TABLE integration_links (
    id             UUID        PRIMARY KEY DEFAULT uuidv7(),
    workspace_id   UUID        NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    integration_id UUID        NOT NULL REFERENCES integrations(id) ON DELETE CASCADE,
    issue_id       UUID        NOT NULL,
    external_type  TEXT        NOT NULL,   -- 'pull_request'|'commit'|'thread'
    external_id    TEXT        NOT NULL,
    external_url   TEXT        NULL,
    metadata       JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- INVARIANT: one link per external object per integration.
    CONSTRAINT integration_links_external_key
        UNIQUE (integration_id, external_type, external_id),

    -- COMPOSITE FK: an external object can only be linked to an issue in the
    -- same workspace as the integration that produced it.
    CONSTRAINT integration_links_issue_fk
        FOREIGN KEY (workspace_id, issue_id)
        REFERENCES issues (workspace_id, id) ON DELETE CASCADE
);

CREATE INDEX integration_links_issue_id_idx ON integration_links (issue_id);


-- ============================================================================
-- SECTION 5 — EVENTS
-- All BIGSERIAL: append-only logs, claimed and checkpointed by integer offset.
-- ============================================================================

CREATE TABLE outbox_events (
    id             BIGSERIAL   PRIMARY KEY,
    workspace_id   UUID        NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    aggregate_type TEXT        NOT NULL,   -- 'issue'|'comment'|'project'
    aggregate_id   UUID        NOT NULL,
    event_type     TEXT        NOT NULL,
    payload        JSONB       NOT NULL,
    -- Written in the same transaction as the business change, dispatched later:
    -- the transactional outbox pattern. NULL dispatched_at == not yet published.
    dispatched_at  TIMESTAMPTZ NULL,
    attempts       INT         NOT NULL DEFAULT 0,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- PARTIAL BY DESIGN. The dispatcher only ever asks "what is undispatched?", so
-- the index covers the PENDING SET rather than lifetime throughput. Dispatched
-- rows drop out of the index on update, keeping it roughly the size of the
-- backlog — the claim query stays flat as the table grows to millions of rows,
-- and index maintenance does not grow with history.
CREATE INDEX outbox_events_pending_idx
    ON outbox_events (id)
    WHERE dispatched_at IS NULL;


CREATE TABLE inbox_events (
    id                BIGSERIAL   PRIMARY KEY,
    provider          TEXT        NOT NULL,
    -- Provider's own event id. Paired with provider it is the idempotency key
    -- for at-least-once webhook delivery: a redelivered event collides on the
    -- UNIQUE below and is dropped rather than processed twice.
    external_event_id TEXT        NOT NULL,
    workspace_id      UUID        NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    event_type        TEXT        NOT NULL,
    payload           JSONB       NOT NULL,
    status            TEXT        NOT NULL DEFAULT 'pending',  -- 'pending'|'processing'|'processed'|'failed'
    attempts          INT         NOT NULL DEFAULT 0,
    error             TEXT        NULL,
    received_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at      TIMESTAMPTZ NULL,

    -- INVARIANT: exactly-once processing of an at-least-once delivery stream.
    CONSTRAINT inbox_events_provider_external_key UNIQUE (provider, external_event_id)
);

COMMENT ON COLUMN inbox_events.workspace_id IS
    'Nullable: an inbound event is recorded before it is routed, and may arrive '
    'from an installation not yet mapped to a workspace.';

-- PARTIAL BY DESIGN, same reasoning as outbox_events_pending_idx. The worker
-- claims from the unprocessed set only; processed rows leave the index, so it
-- tracks the backlog rather than the history.
CREATE INDEX inbox_events_pending_idx
    ON inbox_events (id)
    WHERE status <> 'processed';


CREATE TABLE sync_events (
    id              BIGSERIAL   PRIMARY KEY,
    workspace_id    UUID        NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    -- Gap-free per-tenant cursor allocated from workspaces.sync_seq. Clients
    -- resume with "give me everything > my watermark"; a global id could not
    -- serve that, since a tenant sees only its own slice of the id space.
    publication_seq BIGINT      NOT NULL,
    entity_type     TEXT        NOT NULL,   -- 'issue'|'comment'|'project'|...
    entity_id       UUID        NOT NULL,
    entity_version  INT         NOT NULL,   -- mirrors e.g. issues.version
    action          TEXT        NOT NULL,   -- 'create'|'update'|'delete'
    actor_id        UUID        NULL REFERENCES users(id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- INVARIANT: the per-workspace stream is dense and ordered, so a client can
    -- detect a missed event by a gap in the sequence.
    CONSTRAINT sync_events_workspace_seq_key UNIQUE (workspace_id, publication_seq)
);

-- NO payload column, BY DESIGN. sync_events carries a REFERENCE and a VERSION,
-- never row contents. Three reasons:
--   1. Authorisation is evaluated at read time. A payload baked in at write
--      time would leak fields to a client whose permissions changed since.
--   2. The stream stays small and cheap to fan out to every connected client.
--   3. entity_version lets a client skip straight to current state instead of
--      replaying a chain of deltas.
-- Clients receive (entity_type, entity_id, entity_version) and re-fetch through
-- the normal, permission-checked read path.

-- Realtime catch-up: "events for this workspace above my cursor".
CREATE INDEX sync_events_workspace_seq_idx ON sync_events (workspace_id, publication_seq);


CREATE TABLE issue_history (
    id           BIGSERIAL   PRIMARY KEY,
    workspace_id UUID        NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    issue_id     UUID        NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
    -- Nullable and SET NULL, not RESTRICT: the audit entry must survive the
    -- deletion of the account that caused it.
    actor_id     UUID        NULL REFERENCES users(id) ON DELETE SET NULL,
    field        TEXT        NOT NULL,   -- 'state_id'|'assignee_id'|'title'|...
    old_value    JSONB       NULL,       -- NULL on creation events
    new_value    JSONB       NULL,       -- NULL on clear events
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Activity feed on the issue detail view: newest change first.
CREATE INDEX issue_history_issue_created_idx ON issue_history (issue_id, created_at DESC);

COMMIT;
