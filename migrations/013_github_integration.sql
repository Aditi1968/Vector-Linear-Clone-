-- GitHub App integration: which workspace has installed the app, into which
-- account, and which repositories that installation covers.
--
-- The shape this file exists to make impossible is a secret in a row.
--
-- A GitHub App proves who it is with an RSA private key, and mints a
-- short-lived installation token from that key on demand. Every one of those
-- values is a bearer credential: whoever holds the key can act as the app in
-- every account that has installed it, and whoever holds an installation token
-- can read that account's code until it expires. None of them appears below.
-- The private key and the webhook secret live in configuration
-- (app/config.py), which is read from the process environment and never
-- written anywhere this schema can reach; a backup, a support query or a
-- `SELECT *` in a log therefore discloses no credential at all.
--
-- There is deliberately NO cached-token column. An installation token expires
-- an hour after it is minted, so caching one would buy at most an hour of
-- saved round trips in exchange for putting a live credential in the
-- database, in the backups, and in every replica -- and the row would outlive
-- the token, so the column would spend most of its life holding an expired
-- secret nobody could tell was expired. Tokens are minted per use and held in
-- memory. If that ever has to change, the column must carry its own
-- `expires_at`, must be documented here as short-lived, and must never be
-- selected into anything that reaches the GraphQL layer.
--
-- What IS stored is public or near-public: the numeric installation id GitHub
-- puts in every webhook, the account login the app was installed into, and
-- the repositories that installation covers. None of it authenticates
-- anything on its own.
--
-- Apply this ONLY through `python -m scripts.apply_migration
-- migrations/013_github_integration.sql`. The runner wraps the file in one
-- transaction, takes an advisory lock and records a checksum; a hand-run gets
-- none of that and executes statement-at-a-time in autocommit, which here
-- would mean a `github_repositories` whose parent table exists without its
-- constraints, or the other way round.
--
-- DEPENDS ON 002, which creates `workspaces`, and on 004, which creates
-- `workspace_members`. github_installations_connected_by_fk below references
-- that table's primary key, so applying this file before 004 fails on that
-- constraint and -- inside the single transaction the runner wraps the file
-- in -- leaves nothing behind. That failure IS the dependency check: the
-- ledger records what has been applied and not what depends on what, so the
-- schema itself is what has to refuse an out-of-order apply.


CREATE TABLE github_installations (
    -- The workspace IS the key. One workspace connects one GitHub account,
    -- and a surrogate id beside it would leave "which installation is this
    -- workspace's" answerable by two different lookups, which is how a
    -- workspace acquires two installations and a webhook acquires two
    -- possible destinations.
    --
    -- Reconnecting to a different account is therefore a delete and an
    -- insert rather than a second row, which is what GithubService.connect
    -- does inside one transaction.
    workspace_id UUID PRIMARY KEY,

    -- GitHub's own id for this installation of the app. BIGINT rather than
    -- INTEGER: it is an opaque counter GitHub owns, currently well inside 32
    -- bits and with nothing but the present size of GitHub keeping it there.
    -- Widening a primary-key-adjacent column later is a table rewrite; the
    -- four extra bytes are not worth the migration.
    --
    -- This is the routing key for every webhook: a delivery names an
    -- installation and nothing else, so this column is what turns a signed
    -- payload into a workspace.
    installation_id BIGINT NOT NULL,

    -- The org or user the app was installed into ("acme", "octocat").
    --
    -- Nullable, and it is worth saying why rather than leaving it to be
    -- rediscovered. The setup redirect GitHub sends the installing admin back
    -- with carries `installation_id` and nothing else -- no account, no
    -- repositories -- so at the moment the row is created this value is not
    -- yet known to the server. It is filled in by the first signed
    -- `installation` webhook, which does carry it. A NULL here means
    -- "connected, and GitHub has not told us the account yet", which is a
    -- real and short-lived state, not a missing value to be backfilled.
    account_login TEXT,

    -- Which member connected it. NOT NULL: an integration is always somebody's
    -- act, and a row that could not say whose would make an audit of "who gave
    -- this app access to our code" unanswerable.
    connected_by UUID NOT NULL,

    connected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- RESTRICT on both sides, matching every other foreign key in this schema.
    -- A workspace with a live installation is not something to delete by
    -- accident: the rows would go, the app would stay installed on GitHub, and
    -- its webhooks would arrive for a tenant that no longer exists.
    CONSTRAINT github_installations_workspace_fk
        FOREIGN KEY (workspace_id)
        REFERENCES workspaces (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    -- The connector must be a member of THIS workspace.
    --
    -- `REFERENCES users (id)` is the obvious spelling and it is the bug, for
    -- the reason migrations/009_projects.sql gives at length about
    -- projects_lead_fk: it checks that the id is a real account somewhere and
    -- says nothing about where, so any user from any tenant would be
    -- recordable as the person who connected any workspace's integration. The
    -- reference is composite instead, onto `workspace_members (workspace_id,
    -- user_id)` -- that table's primary key, marked [FK TARGET] in 004 -- and
    -- one `workspace_id` column feeds both this constraint and the workspace
    -- key above, so the connector is checked against the same tenant the
    -- installation belongs to.
    --
    -- Both referencing columns are NOT NULL, so MATCH SIMPLE has no row to
    -- exempt here and the check is unconditional.
    --
    -- ON DELETE RESTRICT: removing the person who connected the integration is
    -- refused while it exists, rather than silently orphaning the record of
    -- who did it. Disconnect first, or reconnect as someone else.
    CONSTRAINT github_installations_connected_by_fk
        FOREIGN KEY (workspace_id, connected_by)
        REFERENCES workspace_members (workspace_id, user_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    -- One installation belongs to one workspace, globally.
    --
    -- This is a tenancy constraint disguised as a uniqueness one. Webhooks are
    -- routed by installation_id, so two workspaces claiming the same id would
    -- make every delivery for it ambiguous -- and the workspace the planner
    -- reached first would receive another tenant's repository names. The
    -- database refusing the second claim is what keeps that from depending on
    -- the application remembering to look.
    CONSTRAINT github_installations_installation_id_key UNIQUE (installation_id),

    -- GitHub's ids are positive counters. A zero or negative value is a
    -- parsing accident -- an empty query parameter read as 0, a sign lost in
    -- transit -- and not an installation anything could route to.
    CONSTRAINT github_installations_installation_id_positive
        CHECK (installation_id > 0),

    -- GitHub's own rule for an account login: alphanumerics and single
    -- hyphens, 39 characters at most. A shape check, not a claim that the
    -- account exists -- only GitHub can say that, and it says it by signing
    -- the webhook this value arrives in.
    CONSTRAINT github_installations_account_login_format
        CHECK (account_login IS NULL OR account_login ~ '^[A-Za-z0-9][A-Za-z0-9-]{0,38}$')
);


-- The repositories one installation covers.
--
-- A table rather than a JSONB column on the row above, because these are rows
-- the product will query: "which workspace owns this repository" is the lookup
-- a commit or a pull-request webhook has to answer, and it wants a key rather
-- than a containment operator over a document. A JSONB array would also have
-- no shape at all -- nothing would stop a null id, a duplicated repository or
-- a full name in the wrong column -- so every constraint below would have to
-- become application code that runs only on the paths that remember to call
-- it.
--
-- No surrogate id: the row IS its key, exactly as project_teams in 009.
CREATE TABLE github_repositories (
    -- One workspace_id for the whole row. It is the tenant, it is the parent
    -- key, and it is the leading column of every read.
    workspace_id UUID NOT NULL,

    -- GitHub's numeric id for the repository, which is what survives a rename
    -- or a transfer. Stored alongside the name rather than instead of it
    -- because the name is what a human reads and the id is what a webhook
    -- matches.
    repository_id BIGINT NOT NULL,

    -- "owner/name" as GitHub last reported it. Denormalised on purpose: it
    -- changes when the repository is renamed, and the rename arrives as a
    -- webhook that rewrites this column. Nothing keys off it.
    full_name TEXT NOT NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- The primary key is also the uniqueness rule ("a repository appears once
    -- per workspace"), also the index that answers "which repositories does
    -- this workspace have", and also the referencing-side index the RESTRICT
    -- below needs -- (workspace_id) is its leading prefix. Three jobs, one
    -- index, which is why there is no CREATE INDEX for this table.
    CONSTRAINT github_repositories_pkey PRIMARY KEY (workspace_id, repository_id),

    -- Points at the installation's key, so a repository cannot exist without
    -- one and cannot belong to a workspace that has not connected the app.
    --
    -- RESTRICT rather than CASCADE, in line with the rest of this schema and
    -- for the reason 009 gives about project_teams: a single `DELETE FROM
    -- github_installations` would otherwise discard every repository row in
    -- the workspace while the command tag read `DELETE 1`. GithubService
    -- deletes these rows itself, first, in the same transaction -- so the
    -- constraint guards that ordering rather than obstructing it.
    CONSTRAINT github_repositories_installation_fk
        FOREIGN KEY (workspace_id)
        REFERENCES github_installations (workspace_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    CONSTRAINT github_repositories_repository_id_positive
        CHECK (repository_id > 0),

    -- owner/name, with GitHub's character rules on each half. A shape check
    -- for the same reason as the account login above: it rejects the plausible
    -- mistakes -- an empty string, a bare name with no owner, a URL, a name
    -- with a space or a second slash -- and leaves the actual guarantee to the
    -- signature on the payload the value arrived in.
    CONSTRAINT github_repositories_full_name_format
        CHECK (full_name ~ '^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$')
);


-- The referencing side of github_installations_connected_by_fk.
--
-- PostgreSQL indexes the REFERENCED side of a foreign key and never the
-- referencing side, so without this every deletion from `workspace_members`
-- scans github_installations in full to satisfy the RESTRICT above -- on the
-- path of every member removal in every workspace. The same index 009 creates
-- for projects.lead_id, for the same reason.
CREATE INDEX github_installations_connected_by_idx
    ON github_installations (workspace_id, connected_by);

-- "Which workspace owns this repository", which is the question a push or
-- pull-request webhook asks. The primary key answers the other direction and
-- cannot answer this one: repository_id is its second column.
--
-- Not unique. The same repository can legitimately be covered by two
-- workspaces' installations if two organisations both grant access to a fork,
-- and refusing that would be a product decision made by an index.
CREATE INDEX github_repositories_repository_id_idx
    ON github_repositories (repository_id);
