-- GitHub development activity: the pull requests and commits an installation
-- reports, and which Vector issues each one is about.
--
-- migrations/013_github_integration.sql ends by saying that "which workspace
-- owns this repository" is "the lookup a commit or a pull-request webhook has
-- to answer", and creates github_repositories_repository_id_idx to answer it.
-- This file is the other half of that sentence: the rows those deliveries
-- write once the workspace is known.
--
-- The shape this file exists to make impossible is a link nobody signed for.
--
-- A pull request says "ENG-142" in its title and a commit says it in its
-- message, and that text is written by whoever opened the pull request --
-- which, on a public repository, is anybody at all. So the identifier in a
-- payload is a REQUEST to link, never a proof of one. Every row below is
-- keyed through `workspace_id`, and every foreign key out of it is composite,
-- so a link can only ever land on an issue in the same workspace as the
-- repository the delivery came from. An attacker who opens a pull request
-- titled "ENG-142" on their own fork gets a link to *their* workspace's
-- ENG-142 or to nothing -- there is no column left for a second workspace to
-- go in. The resolution itself (identifier text to issue id) is the service's
-- job and is scoped there too; this schema is what makes a mistake in that
-- code unstorable rather than merely unlikely.
--
-- The second shape it makes impossible is a delivery applied twice. GitHub
-- retries a delivery it did not see a 2xx for, and a retry of `push` that
-- re-ran its writes would be harmless only for as long as every write stays
-- idempotent. `github_deliveries` records the id GitHub stamps on each one and
-- makes the second attempt a primary-key violation rather than a second
-- application.
--
-- Apply this ONLY through `python -m scripts.apply_migration
-- migrations/017_github_development.sql`. The runner wraps the file in one
-- transaction, takes an advisory lock and records a checksum; a hand-run gets
-- none of that and executes statement-at-a-time in autocommit, which here
-- would mean join tables whose parents exist without their constraints.
--
-- DEPENDS ON 013, for `github_repositories` and the installation row its
-- primary key hangs from, and on 007, for `issues_workspace_id_key` -- the
-- UNIQUE (workspace_id, id) on `issues` that both join tables below reference.
-- Applying this file before either fails on that constraint and -- inside the
-- single transaction the runner wraps the file in -- leaves nothing behind.
-- That failure IS the dependency check: the ledger records what has been
-- applied and not what depends on what, so the schema itself is what has to
-- refuse an out-of-order apply.


-- Every delivery this server has already applied, by the id GitHub stamps on
-- it (the X-GitHub-Delivery header).
--
-- NOT workspace-scoped, and that is the point rather than an oversight. The
-- duplicate check has to run before the payload is parsed and therefore before
-- the workspace is known -- routing a delivery costs a lookup on
-- `installation_id`, and doing that work for a redelivery this server has
-- already applied is exactly what this table exists to avoid. A delivery id is
-- GitHub's own value and unique across all of GitHub, so a global key is the
-- honest shape for it.
--
-- TEXT rather than UUID. GitHub currently sends a UUID and documents nothing
-- that promises to keep doing so; a UUID column would turn a format change at
-- the provider into a 500 on every delivery. The CHECK below bounds the length
-- instead, which is the property that actually matters here.
CREATE TABLE github_deliveries (
    delivery_id TEXT PRIMARY KEY,

    -- What arrived, kept for operator diagnosis rather than for any query the
    -- product runs. Nullable: a delivery with no X-GitHub-Event header is
    -- malformed, and recording that it was seen still beats applying it twice.
    event TEXT,

    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- A bound, not a validation. The id is used as a key and is echoed into no
    -- response, so its content is not a trust question -- but an unbounded
    -- TEXT primary key is a way to make an index enormous by sending a
    -- megabyte header, and 200 is two orders of magnitude above the 36 GitHub
    -- actually sends.
    CONSTRAINT github_deliveries_delivery_id_length
        CHECK (length(delivery_id) BETWEEN 1 AND 200)
);

-- The pruning index. This table grows by one row per delivery forever and
-- nothing in the product reads it by anything but its primary key, so the only
-- query it will ever face besides the idempotency probe is "delete everything
-- older than N days".
--
-- ponytail: no pruning job ships with this migration. The table is small
-- (roughly one row per push and per pull-request event) and a scheduled
-- DELETE has nowhere to live until the reminder/recurring scheduler in 019
-- exists. Add the sweep there; this index is what makes it cheap when it
-- arrives.
CREATE INDEX github_deliveries_received_at_idx ON github_deliveries (received_at);


CREATE TABLE github_pull_requests (
    -- One workspace_id for the whole row, feeding both the repository key
    -- below and -- through the join table -- the issues it links to.
    workspace_id UUID NOT NULL,

    repository_id BIGINT NOT NULL,

    -- The number a human reads: "#84". Unique within its repository and
    -- assigned by GitHub, never reused, and shared with issues on the same
    -- repository (GitHub numbers both from one sequence).
    number INTEGER NOT NULL,

    title TEXT NOT NULL,

    -- GitHub's own two-state field, stored as GitHub sends it.
    --
    -- Deliberately NOT the five-state enum a settings page wants to show
    -- (draft / open / ready_for_review / merged / closed). GitHub does not
    -- have such a field: it has `state` (open or closed), a `draft` boolean
    -- and a `merged_at` instant, and a merged pull request arrives as
    -- state=closed with merged_at set. Flattening those three into one column
    -- means choosing a precedence here, in SQL, and then being unable to
    -- answer "was this closed or merged" when the presentation later needs
    -- both. The three source fields are stored; the display state is derived
    -- in app/domain/github.py, where a precedence change is a code change with
    -- a test rather than a migration.
    state TEXT NOT NULL,

    draft BOOLEAN NOT NULL DEFAULT FALSE,

    -- When it merged, or NULL for one that has not. Also the answer to "was
    -- this closed or merged", which `state` alone cannot give.
    merged_at TIMESTAMPTZ,

    -- The branch the pull request is FROM ("eng-142-fix-slack-oauth"), which
    -- is what the Development section shows next to the branch-name helper.
    -- Nullable: a payload from a deleted or cross-fork head can omit it.
    head_ref TEXT,

    -- The html_url GitHub reports. Stored rather than rebuilt from owner, name
    -- and number because GitHub owns its URL layout and a rebuilt link is a
    -- guess that breaks silently; a link the provider handed us is the one
    -- that keeps working.
    url TEXT,

    -- GitHub's timestamps, not ours. `updated_at` is what makes an
    -- out-of-order redelivery detectable: two deliveries for one pull request
    -- can arrive in either order, and the later payload is the one whose
    -- `updated_at` is greater, not the one that arrived second.
    github_updated_at TIMESTAMPTZ,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- The pull request IS its (repository, number). A surrogate id beside that
    -- would leave "which row is pull request #84" answerable two ways, which
    -- is how a redelivery becomes a second row. The key is also the index the
    -- upsert path looks the row up by.
    CONSTRAINT github_pull_requests_pkey
        PRIMARY KEY (workspace_id, repository_id, number),

    -- The pull request's repository must be one this workspace's installation
    -- actually covers. Composite through workspace_id, so a delivery cannot
    -- file a pull request against another tenant's repository row, and
    -- referencing github_repositories_pkey -- which is exactly this pair.
    --
    -- RESTRICT rather than CASCADE, in line with the rest of this schema and
    -- for the reason 013 gives about github_repositories: a repository
    -- disappearing from an installation must not silently discard the
    -- development history attached to it while the command tag reads
    -- `DELETE 1`. GithubService removes these rows itself, first, in the same
    -- transaction.
    CONSTRAINT github_pull_requests_repository_fk
        FOREIGN KEY (workspace_id, repository_id)
        REFERENCES github_repositories (workspace_id, repository_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    CONSTRAINT github_pull_requests_state_check
        CHECK (state IN ('open', 'closed')),

    -- GitHub numbers from 1 upward. A zero is a parsing accident -- an absent
    -- key read as 0 -- and not a pull request anything could link to.
    CONSTRAINT github_pull_requests_number_positive CHECK (number > 0),

    -- An open pull request has not merged. The combination state='open' with a
    -- merged_at is not a state GitHub produces, and storing it would make the
    -- derived display state in the domain answer something no payload said.
    CONSTRAINT github_pull_requests_merged_is_closed
        CHECK (merged_at IS NULL OR state = 'closed'),

    CONSTRAINT github_pull_requests_title_length
        CHECK (length(title) BETWEEN 1 AND 1024),

    -- Git's own rule for a ref, loosely: no spaces, no control characters, and
    -- bounded. A shape check rather than a claim the branch exists -- only
    -- GitHub can say that, and it says it by signing the payload this value
    -- arrived in.
    CONSTRAINT github_pull_requests_head_ref_format
        CHECK (head_ref IS NULL OR head_ref ~ '^[!-~]{1,255}$'),

    CONSTRAINT github_pull_requests_url_length
        CHECK (url IS NULL OR length(url) BETWEEN 1 AND 2048),

    -- [FK TARGET] What github_pull_request_issues references. Redundant beside
    -- the primary key only if you do not look at what points here: a foreign
    -- key may reference a UNIQUE column set and nothing else, and the primary
    -- key already is one -- so this is the primary key doing that second job,
    -- named here for the reader rather than declared twice.
    CONSTRAINT github_pull_requests_workspace_repository_number_key
        UNIQUE (workspace_id, repository_id, number)
);


-- Which Vector issues a pull request is about.
--
-- A join table rather than an `issue_id` column on the row above, because one
-- pull request routinely closes several issues ("Fixes ENG-1, ENG-2") and a
-- single column would make the second identifier in a title silently
-- unrecordable. No surrogate id: the row IS its key, exactly as project_teams
-- in 009 and github_repositories in 013.
CREATE TABLE github_pull_request_issues (
    -- One workspace_id, read by BOTH foreign keys below. That is the entire
    -- cross-tenant guarantee: the pull request and the issue are checked
    -- against the SAME tenant rather than against two that happen to be
    -- spelled separately. A pair of single-column keys -- number -> pull
    -- requests, issue_id -> issues.id -- would accept a row pairing workspace
    -- A's pull request with workspace B's issue, and neither constraint would
    -- notice. That row is precisely what a hostile pull-request title is
    -- trying to create.
    workspace_id UUID NOT NULL,

    repository_id BIGINT NOT NULL,
    number INTEGER NOT NULL,

    issue_id UUID NOT NULL,

    -- How the link was made: 'title', 'body', or 'branch'. Kept because the
    -- three are not equally strong evidence -- a branch name is chosen by
    -- someone with push access, a body can be edited by anyone who can comment
    -- -- and a future rule that trusts them differently needs to know which
    -- one it is looking at. Also what lets an edited body remove a link it
    -- previously added without disturbing one the branch name still supports.
    source TEXT NOT NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- The key is also the uniqueness rule ("a pull request links to an issue
    -- once per source") and also the index answering "what does this pull
    -- request link to". `source` is inside it so that a title link and a
    -- branch link to the same issue are two rows: removing the title link when
    -- the title is edited must not remove the branch's.
    CONSTRAINT github_pull_request_issues_pkey
        PRIMARY KEY (workspace_id, repository_id, number, issue_id, source),

    CONSTRAINT github_pull_request_issues_pull_request_fk
        FOREIGN KEY (workspace_id, repository_id, number)
        REFERENCES github_pull_requests (workspace_id, repository_id, number)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    -- The issue must be in the SAME workspace, through the same column. This
    -- is the constraint the file's opening paragraph is about.
    CONSTRAINT github_pull_request_issues_issue_fk
        FOREIGN KEY (workspace_id, issue_id)
        REFERENCES issues (workspace_id, id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    CONSTRAINT github_pull_request_issues_source_check
        CHECK (source IN ('title', 'body', 'branch'))
);


CREATE TABLE github_commits (
    workspace_id UUID NOT NULL,
    repository_id BIGINT NOT NULL,

    -- The full 40-character SHA-1, lowercase. Full rather than the short form
    -- the UI shows: the abbreviation is a display concern and is ambiguous by
    -- construction, so a key built on it would collide in exactly the
    -- repositories big enough for it to matter.
    sha TEXT NOT NULL,

    message TEXT NOT NULL,

    url TEXT,

    -- When the commit was authored, as git records it. Nullable because a
    -- push payload can omit it, and NULL orders last wherever commits are
    -- listed rather than pretending to be the epoch.
    committed_at TIMESTAMPTZ,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT github_commits_pkey PRIMARY KEY (workspace_id, repository_id, sha),

    CONSTRAINT github_commits_repository_fk
        FOREIGN KEY (workspace_id, repository_id)
        REFERENCES github_repositories (workspace_id, repository_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    -- Exactly 40 lowercase hex characters. GitHub sends SHA-1 today; a
    -- repository on SHA-256 would send 64 and fail this check loudly, which is
    -- the right failure -- it is a schema change with a considered index and
    -- display story, not something to accept silently into a column the UI
    -- abbreviates to seven characters.
    CONSTRAINT github_commits_sha_format CHECK (sha ~ '^[0-9a-f]{40}$'),

    CONSTRAINT github_commits_message_length
        CHECK (length(message) BETWEEN 1 AND 8192),

    CONSTRAINT github_commits_url_length
        CHECK (url IS NULL OR length(url) BETWEEN 1 AND 2048),

    -- [FK TARGET] What github_commit_issues references, for the same reason
    -- the pull-request table names its primary key as one.
    CONSTRAINT github_commits_workspace_repository_sha_key
        UNIQUE (workspace_id, repository_id, sha)
);


-- Which Vector issues a commit is about. The commit-side twin of
-- github_pull_request_issues, and the same tenancy argument applies to it in
-- full: one workspace_id, read by both foreign keys.
CREATE TABLE github_commit_issues (
    workspace_id UUID NOT NULL,
    repository_id BIGINT NOT NULL,
    sha TEXT NOT NULL,

    issue_id UUID NOT NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- No `source` column here, unlike the pull-request join. A commit has one
    -- place an identifier can appear -- its message -- and a commit message is
    -- immutable, so there is no second source to distinguish and no edit that
    -- could retract one.
    CONSTRAINT github_commit_issues_pkey
        PRIMARY KEY (workspace_id, repository_id, sha, issue_id),

    CONSTRAINT github_commit_issues_commit_fk
        FOREIGN KEY (workspace_id, repository_id, sha)
        REFERENCES github_commits (workspace_id, repository_id, sha)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    CONSTRAINT github_commit_issues_issue_fk
        FOREIGN KEY (workspace_id, issue_id)
        REFERENCES issues (workspace_id, id)
        ON DELETE RESTRICT ON UPDATE RESTRICT
);


-- "What development activity does this issue have", which is the only question
-- the Development section in the issue inspector asks, and it asks it from the
-- issue side -- the opposite direction to both join tables' primary keys.
--
-- Without these two indexes that panel scans every link row in the workspace
-- on every issue open. They are also the referencing side of the two
-- `issue_fk` constraints above: PostgreSQL indexes the REFERENCED side of a
-- foreign key and never the referencing side, so without them every
-- `DELETE FROM issues` scans both join tables in full to satisfy the RESTRICT.
-- The same index 009 creates for projects.lead_id, for both of the same
-- reasons.
CREATE INDEX github_pull_request_issues_workspace_issue_idx
    ON github_pull_request_issues (workspace_id, issue_id);

CREATE INDEX github_commit_issues_workspace_issue_idx
    ON github_commit_issues (workspace_id, issue_id);

-- The referencing side of github_pull_requests_repository_fk and
-- github_commits_repository_fk. (workspace_id, repository_id) is the leading
-- prefix of both tables' primary keys, so both RESTRICT checks are already
-- served and neither table needs an index of its own here.
--
-- What is NOT served by any key above is "this repository's commits, newest
-- first", which is what a repository-scoped view would ask. No such view
-- exists yet, so no index is created for it -- an index for a query nobody
-- runs is a write cost with no reader.

-- Pull requests in the order the Development section lists them: newest
-- activity first, within one workspace's repository. `number DESC` is the
-- tie-break that makes the order total when two payloads share an
-- `updated_at`, which redeliveries of one edit routinely do.
CREATE INDEX github_pull_requests_workspace_repository_updated_idx
    ON github_pull_requests (workspace_id, repository_id, github_updated_at DESC, number DESC);
