-- Releases: what shipped, where it shipped to, and the notes that describe it.
--
-- The shape this file exists to make impossible is a release note that
-- changes after it was published.
--
-- migrations/017_github_development.sql already stores the raw material --
-- `github_commits`, `github_pull_requests` and the two join tables saying
-- which Vector issues each one is about. A release is a *reading* of that
-- material over a range: "everything between the commit we last deployed and
-- the commit we are deploying now". Nothing below copies those tables, and
-- nothing below re-derives that reading on the way out.
--
-- It is worth being exact about why the reading is frozen rather than
-- computed on read, because a view would be a smaller diff and it would be
-- wrong. 017's tables keep moving: a push webhook arrives late, a body is
-- edited and a link is retracted, a repository is renamed, a redelivery lands
-- out of order. A `Release.notes` computed from a live window would therefore
-- answer differently tomorrow than it did at deploy time -- so the document
-- that went out with v1.4.0, that somebody pasted into a changelog and that an
-- auditor may later be asked to reconcile, would silently disagree with the
-- product. A release note is a statement made at a moment. It is stored as
-- one: `releases.notes` is text written once, and `release_issues` /
-- `release_pull_requests` are the structured record of what that text was
-- built from.
--
-- That is also what makes the generation DETERMINISTIC in the sense that
-- matters. The rendering itself is a pure function in app/domain/releases.py
-- with no clock, no randomness and no ordering taken from the database -- it
-- sorts its own inputs -- so the same inputs produce the same bytes. This
-- schema supplies the other half: the inputs stop changing the instant the
-- release row is written.
--
-- The second shape it makes impossible is the usual one, and it is the reason
-- every key below is composite through `workspace_id`. A release names a
-- repository, an environment, a set of issues and a set of pull requests, and
-- every one of those ids reaches this layer from a browser. There is no column
-- for a second workspace to go in, so a release cannot cite another tenant's
-- repository, deploy to another tenant's environment, or claim to have shipped
-- another tenant's issue -- whatever a resolver forgets.
--
-- Apply this ONLY through `python -m scripts.apply_migration
-- migrations/024_releases.sql`. The runner wraps the file in one transaction,
-- takes an advisory lock and records a checksum; a hand-run gets none of that
-- and executes statement-at-a-time in autocommit, which here would mean join
-- tables whose parents exist without their constraints.
--
-- DEPENDS ON 002, for `workspaces`; on 007, for `issues_workspace_id_key` --
-- the UNIQUE (workspace_id, id) on `issues` that `release_issues` references;
-- and on 013, for `github_repositories_pkey`. Applying this file before any of
-- them fails on that constraint and -- inside the single transaction the
-- runner wraps the file in -- leaves nothing behind. That failure IS the
-- dependency check: the ledger records what has been applied and not what
-- depends on what, so the schema itself is what has to refuse an out-of-order
-- apply.
--
-- It does NOT depend on 017 through a foreign key, and the absence is
-- deliberate; `releases.commit_sha` below says why at length.


-- Where a release goes.
--
-- `environments` rather than `release_environments`: an environment is a
-- deploy target, not a property of the releases feature. The first thing that
-- needed one was releases, but a feature flag, a synthetic check or an
-- environment-scoped setting all name the same rows, and a table prefixed with
-- whichever feature happened to arrive first is a table the second feature has
-- to explain.
CREATE TABLE environments (
    -- One workspace_id for the whole row. It is the tenant, it is the parent
    -- key, and it is the leading column of every read.
    workspace_id UUID NOT NULL,

    id UUID NOT NULL DEFAULT uuidv7(),

    -- What a human calls it: "Production", "Staging EU", "Ada's laptop".
    name TEXT NOT NULL,

    -- What it IS: development, staging, production or custom.
    --
    -- Stored beside the name rather than derived from it, because the two
    -- answer different questions and only one of them is a rule. The name is a
    -- label and a workspace may have several production environments -- "Prod
    -- EU" and "Prod US" are both production, and a rule written against the
    -- string "production" would recognise neither. `kind` is what a future
    -- policy keys off ("a deploy to production requires an approval", "show
    -- the production banner in red"), and it stays true through a rename.
    --
    -- TEXT with a CHECK and not an enum, matching every other vocabulary in
    -- this schema. A PostgreSQL enum cannot have a value removed and cannot
    -- have one reordered without a rewrite, and every consumer of this column
    -- already has to know the strings.
    --
    -- 'custom' is a real member and not an escape hatch: a preview environment
    -- per pull request, a load-test rig, a customer-specific sandbox. Without
    -- it those would each have to be miscategorised as one of the other three,
    -- and the production rule above would then be enforced against a rig.
    kind TEXT NOT NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- [FK TARGET] What releases_environment_fk references, and also the index
    -- that answers "which environments does this workspace have" -- the only
    -- read this table has. `workspace_id` leads it, so the RESTRICT below is
    -- served by its leading prefix too and this table needs no CREATE INDEX.
    CONSTRAINT environments_pkey PRIMARY KEY (workspace_id, id),

    CONSTRAINT environments_workspace_fk
        FOREIGN KEY (workspace_id)
        REFERENCES workspaces (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    -- One name per workspace. Two environments called "Production" is not a
    -- configuration anyone meant to have: whichever one a deploy script picked
    -- would be the one nobody was watching. Deliberately NOT unique on `kind`
    -- -- see the note on that column, a workspace may legitimately run several
    -- production environments.
    CONSTRAINT environments_workspace_name_key UNIQUE (workspace_id, name),

    CONSTRAINT environments_kind_check
        CHECK (kind IN ('development', 'staging', 'production', 'custom')),

    CONSTRAINT environments_name_length CHECK (length(name) BETWEEN 1 AND 100)
);


CREATE TABLE releases (
    -- One workspace_id, read by every foreign key on this row and on both
    -- tables below it.
    workspace_id UUID NOT NULL,

    id UUID NOT NULL DEFAULT uuidv7(),

    -- The version a human reads: "v1.4.0", "2026-09-07.2", "hotfix-oauth".
    --
    -- Free text and NOT unique, which is a decision rather than an omission.
    -- The same version legitimately appears more than once: v1.4.0 goes to
    -- staging and then to production, and after a rollback it goes to
    -- production a second time. A UNIQUE here would refuse the redeploy --
    -- which is a product decision made by an index, exactly what
    -- `github_repositories_repository_id_idx` in 013 declines to do.
    name TEXT NOT NULL,

    environment_id UUID NOT NULL,

    -- Which repository shipped. GitHub's numeric id, through the real
    -- composite key of `github_repositories` -- so the repository is checked
    -- against the SAME tenant as the release rather than against one that
    -- happens to be spelled separately.
    repository_id BIGINT NOT NULL,

    -- The commit that shipped, and the commit the last deploy left behind.
    -- Together they are the RANGE the notes were generated from:
    -- `(previous_commit_sha, commit_sha]`.
    --
    -- Full 40-character SHAs, for the reason `github_commits.sha` is one: the
    -- abbreviation is ambiguous by construction.
    --
    -- `previous_commit_sha` is NULL for the first release of a repository into
    -- an environment, which is a real state and not a missing value: there is
    -- no lower bound, so the range is everything up to `commit_sha`.
    --
    -- NEITHER IS A FOREIGN KEY ONTO `github_commits`, and that is worth
    -- stating because it is the obvious spelling and it is wrong here. Two
    -- reasons, in increasing order of importance:
    --
    -- * A commit row can be removed. GithubRepository.delete_development drops
    --   `github_commits` when an installation stops covering a repository, and
    --   an FK would turn that webhook into a RestrictViolationError -- a
    --   provider event failing because of a record the provider knows nothing
    --   about.
    -- * A release note is a document, not a view. A release that shipped is a
    --   fact about the past, and it must survive the integration being
    --   disconnected, the repository being deleted on GitHub, and the whole
    --   development history being re-imported. Pinning it to a row that can
    --   vanish would make "what did we ship in v1.4.0" depend on the current
    --   state of a third-party integration.
    --
    -- What replaces the constraint is a scoped lookup at write time:
    -- ReleaseService.create resolves this SHA through
    -- `github_commits (workspace_id = scope, repository_id, sha)` before it
    -- writes anything, so a commit belonging to another tenant is not found
    -- and the release is refused with the same answer a nonexistent SHA gets.
    -- That check is weaker than a constraint by exactly one property -- it
    -- binds only callers that perform it -- and the thing it is protecting
    -- (that the range resolves to something) is not a tenancy property at all.
    -- Tenancy is held by `releases_repository_fk` below and by
    -- `release_issues_issue_fk`, both of which are real constraints.
    commit_sha TEXT NOT NULL,
    previous_commit_sha TEXT,

    -- Where the release is in its lifecycle. Four values, and the vocabulary
    -- is argued rather than borrowed:
    --
    -- pending      the release has been cut and has not reached the
    --              environment. Notes exist -- they were generated when the
    --              row was written -- but nothing has been deployed.
    -- deployed     it reached the environment and is what is running there.
    -- failed       the deploy did not complete. Terminal: a retry is a new
    --              release, because a retry of a deploy that got half way is
    --              not the same act as the first attempt and conflating them
    --              loses the first attempt's timestamp.
    -- rolled_back  it was deployed and has since been withdrawn. Terminal.
    --
    -- There is deliberately NO 'deploying'. A state has to be written by
    -- somebody, and nothing in this system observes a deploy in flight: no CI
    -- integration reports progress, and no webhook says "started". A state
    -- that no writer produces is a state every reader has to handle for
    -- nothing, and the first person to add a CI integration would find it
    -- already occupied by a value that never meant what they need it to.
    --
    -- 'rolled_back' is a status on the withdrawn release rather than a new
    -- release row, because the question it answers is about THAT release:
    -- "is v1.4.0 what is running in production?" The rollback deploy itself is
    -- an ordinary release of the earlier version, and gets its own row.
    --
    -- Which transitions are legal is NOT expressed here. A CHECK cannot read
    -- the previous value of a row, so the rule lives in
    -- `app.domain.releases.RELEASE_TRANSITIONS` and is applied by
    -- ReleaseService.set_status inside one statement that names the states it
    -- will move from. What the database holds is the vocabulary and the
    -- agreement below between the status and the timestamp.
    status TEXT NOT NULL,

    -- The generated release note, frozen at creation.
    --
    -- Derived data in a column, which is normally a smell, and here is the
    -- entire point -- see the file header. NOT NULL: a release always has
    -- notes, and the renderer is total (a range that touched nothing renders
    -- as text saying so), so there is no state this could be null in.
    --
    -- No `notes_edited` flag and no separate `notes_override`. Editing is not
    -- a capability this migration ships, and a column for it would be one
    -- nothing writes and every reader has to branch on.
    notes TEXT NOT NULL,

    -- When it reached the environment, or NULL if it never has.
    --
    -- Not `now()` on insert, and not the same thing as `created_at`: a release
    -- is frequently cut minutes or days before it is deployed, and a column
    -- that conflated the two would make "how long was v1.4.0 in production"
    -- unanswerable. Preserved through a rollback -- a withdrawn release still
    -- WAS deployed, and the instant is what an incident review reads.
    deployed_at TIMESTAMPTZ,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- [FK TARGET] What release_issues and release_pull_requests reference.
    -- `workspace_id` leads it, so it is also the index every workspace-scoped
    -- read of a single release uses.
    CONSTRAINT releases_pkey PRIMARY KEY (workspace_id, id),

    -- The environment must be one THIS workspace declared. Composite through
    -- the one `workspace_id`, so a deploy cannot be filed against another
    -- tenant's environment however the id was obtained.
    --
    -- RESTRICT rather than CASCADE, in line with the rest of this schema: an
    -- environment being removed must not silently discard the history of
    -- everything ever deployed to it while the command tag reads `DELETE 1`.
    CONSTRAINT releases_environment_fk
        FOREIGN KEY (workspace_id, environment_id)
        REFERENCES environments (workspace_id, id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    -- The repository must be one this workspace's installation covers, through
    -- `github_repositories_pkey` -- which is exactly this pair.
    --
    -- RESTRICT, and this one has a consequence worth writing down rather than
    -- discovering: a workspace holding releases cannot disconnect its GitHub
    -- integration or drop a covered repository until those releases are
    -- deleted, because `GithubService.disconnect` deletes `github_repositories`
    -- rows and this constraint refuses while a release names one. That is the
    -- correct default -- deleting shipping history as a side effect of
    -- reconnecting an integration would be the worse failure -- and
    -- `releaseDelete` is the path out of it.
    CONSTRAINT releases_repository_fk
        FOREIGN KEY (workspace_id, repository_id)
        REFERENCES github_repositories (workspace_id, repository_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    CONSTRAINT releases_status_check
        CHECK (status IN ('pending', 'deployed', 'failed', 'rolled_back')),

    -- The status and the timestamp are one fact, so they cannot disagree.
    --
    -- An equality between two booleans rather than two implications, because
    -- both directions are mistakes: a release reported as deployed with no
    -- instant would make "what is running, and since when" unanswerable, and a
    -- pending release carrying a deploy instant would claim a deploy that has
    -- not happened. `rolled_back` is on the left-hand side because a withdrawn
    -- release was deployed once and the instant it happened is preserved.
    CONSTRAINT releases_deployed_at_matches_status
        CHECK ((status IN ('deployed', 'rolled_back')) = (deployed_at IS NOT NULL)),

    CONSTRAINT releases_name_length CHECK (length(name) BETWEEN 1 AND 200),

    -- Exactly 40 lowercase hex characters, restating
    -- `github_commits_sha_format`. There is no foreign key onto that table
    -- (see the column note), so this is what stops an abbreviation, a branch
    -- name or a URL being stored in a column the range resolver will look up
    -- by equality and silently find nothing for.
    CONSTRAINT releases_commit_sha_format CHECK (commit_sha ~ '^[0-9a-f]{40}$'),

    CONSTRAINT releases_previous_commit_sha_format
        CHECK (previous_commit_sha IS NULL OR previous_commit_sha ~ '^[0-9a-f]{40}$'),

    -- A range whose ends are the same commit contains nothing, so a release
    -- spelled that way is a caller passing one SHA twice rather than a deploy
    -- of no changes -- which is spelled `previous_commit_sha = <the previous
    -- deploy's>` and produces empty notes honestly.
    CONSTRAINT releases_previous_commit_differs
        CHECK (previous_commit_sha IS NULL OR previous_commit_sha <> commit_sha),

    -- A bound, not a validation. The notes are generated by this server from
    -- rows it already bounds, so the content is not in question -- but the
    -- inputs include pull-request and issue titles written by other people,
    -- and an unbounded TEXT column is how a hundred of those become a row no
    -- response can carry.
    CONSTRAINT releases_notes_length CHECK (length(notes) BETWEEN 1 AND 100000)
);


-- Which Vector issues one release shipped.
--
-- Resolved once, from the commits and merged pull requests in the release's
-- range, and then left alone -- see the file header. This is the structured
-- half of the frozen note: `releases.notes` is what a human reads, and these
-- rows are what "which release shipped ENG-142?" is answered from.
--
-- No surrogate id: the row IS its key, exactly as `github_pull_request_issues`
-- in 017 and `project_teams` in 009.
CREATE TABLE release_issues (
    -- One workspace_id, read by BOTH foreign keys below. That is the entire
    -- cross-tenant guarantee: the release and the issue are checked against
    -- the SAME tenant rather than against two that happen to be spelled
    -- separately. A pair of single-column keys -- release_id -> releases.id,
    -- issue_id -> issues.id -- would accept a row pairing workspace A's
    -- release with workspace B's issue, and neither constraint would notice.
    --
    -- That row is not hypothetical. The issues here are resolved from
    -- identifiers that arrived in commit messages and pull-request titles,
    -- which 017's header explains at length are written by whoever opened the
    -- pull request -- on a public repository, anybody. A release note is a
    -- document that gets pasted into a changelog and sent to customers, so a
    -- link that reached this table from a hostile title would publish another
    -- tenant's issue title. There is no column for the second workspace to go
    -- in.
    workspace_id UUID NOT NULL,

    release_id UUID NOT NULL,
    issue_id UUID NOT NULL,

    -- The key is also the uniqueness rule ("an issue appears once per
    -- release") and also the index answering "what did this release ship".
    CONSTRAINT release_issues_pkey PRIMARY KEY (workspace_id, release_id, issue_id),

    -- RESTRICT, so deleting a release does not silently discard what it said
    -- it shipped. ReleaseService.delete removes these rows itself, first, in
    -- the same transaction.
    CONSTRAINT release_issues_release_fk
        FOREIGN KEY (workspace_id, release_id)
        REFERENCES releases (workspace_id, id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    -- The issue must be in the SAME workspace, through the same column. This
    -- is the constraint this table's opening paragraph is about.
    CONSTRAINT release_issues_issue_fk
        FOREIGN KEY (workspace_id, issue_id)
        REFERENCES issues (workspace_id, id)
        ON DELETE RESTRICT ON UPDATE RESTRICT
);


-- Which pull requests one release shipped.
--
-- The columns are a SNAPSHOT and not a reference, which is the one place this
-- schema deliberately denormalises. `title` and `url` are copied out of
-- `github_pull_requests` at the moment the release is cut, and there is no
-- foreign key back to that table.
--
-- The reasoning is `releases.commit_sha`'s, applied one table down and one
-- step further. A pull request row is removed when an installation stops
-- covering its repository, and a title is rewritten whenever somebody edits
-- it -- so a reference would make a shipped release's list of pull requests
-- both deletable and mutable by a third party after the fact. The frozen note
-- in `releases.notes` already quotes these titles; storing anything else here
-- would mean the structured record and the text could disagree about the same
-- release.
--
-- What is NOT copied is the state (open / closed / merged / draft). Only
-- merged pull requests enter a release, so the state at that moment is known,
-- and a copy of it would be a value that stops being true the first time
-- somebody reopens one.
--
-- `title` and `url` have NO READER TODAY -- the GraphQL type publishes
-- `pullRequestNumbers`, and the titles a client renders come out of
-- `releases.notes`. They are captured anyway, and the reason is that they
-- cannot be captured later: they are a snapshot of a mutable, deletable,
-- third-party string at one instant, so a column added in a year has nothing
-- to backfill from for every release cut before it. Deleting them as dead
-- weight is the one change to this table that cannot be undone.
CREATE TABLE release_pull_requests (
    workspace_id UUID NOT NULL,
    release_id UUID NOT NULL,

    -- (repository_id, number) is how GitHub identifies a pull request, kept
    -- whole so a future join back to `github_pull_requests` -- for a live
    -- state, on a screen that wants one -- has both halves of the key. A
    -- release names one repository, so this column equals `releases`'
    -- today; it is stored anyway, because a release spanning two repositories
    -- is a plausible next feature and a bare `number` would be ambiguous the
    -- moment it arrives.
    repository_id BIGINT NOT NULL,
    number INTEGER NOT NULL,

    title TEXT NOT NULL,
    url TEXT,

    CONSTRAINT release_pull_requests_pkey
        PRIMARY KEY (workspace_id, release_id, repository_id, number),

    CONSTRAINT release_pull_requests_release_fk
        FOREIGN KEY (workspace_id, release_id)
        REFERENCES releases (workspace_id, id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    -- GitHub numbers from 1 upward, restating
    -- `github_pull_requests_number_positive`. A zero is a parsing accident --
    -- an absent key read as 0 -- and not a pull request anything shipped.
    CONSTRAINT release_pull_requests_number_positive CHECK (number > 0),

    CONSTRAINT release_pull_requests_title_length
        CHECK (length(title) BETWEEN 1 AND 1024),

    CONSTRAINT release_pull_requests_url_length
        CHECK (url IS NULL OR length(url) BETWEEN 1 AND 2048)
);


-- "This workspace's releases, newest first", which is the list screen.
--
-- The same `(workspace_id, created_at DESC, id DESC)` shape 002 gives issues
-- and 022 gives initiatives, for the same reason: it is the keyset the cursor
-- pages by, and `workspace_id` leads it so a page walk cannot leave the
-- tenant.
CREATE INDEX releases_workspace_created_at_id_idx
    ON releases (workspace_id, created_at DESC, id DESC);

-- The referencing side of releases_environment_fk.
--
-- PostgreSQL indexes the REFERENCED side of a foreign key and never the
-- referencing side, so without this every deletion from `environments` scans
-- `releases` in full to satisfy the RESTRICT. It also answers "what has been
-- deployed to this environment", which is the second thing the list screen
-- filters by. The same index 009 creates for projects.lead_id, for both of the
-- same reasons.
CREATE INDEX releases_workspace_environment_idx
    ON releases (workspace_id, environment_id);

-- "What was last deployed to this environment from this repository", which is
-- the lookup that makes a release a RANGE rather than a snapshot: cutting
-- v1.4.0 starts from wherever the previous deploy left off, and
-- ReleaseService.create resolves that here rather than making a client
-- remember it.
--
-- `deployed_at DESC` with `id DESC` as the tie-break, so the answer is total
-- -- two deploys recorded in one transaction share an instant, and without the
-- tie-break "the previous release" would depend on the scan order, which is
-- the subtle way a release note comes out containing somebody else's changes.
--
-- Its leading `(workspace_id, repository_id)` prefix is also the referencing
-- side of releases_repository_fk, so that RESTRICT is served by this index and
-- `github_repositories` needs no second one here.
CREATE INDEX releases_workspace_repository_environment_deployed_idx
    ON releases (workspace_id, repository_id, environment_id, deployed_at DESC, id DESC);

-- "Which releases shipped this issue", which is the opposite direction to
-- release_issues_pkey and is what an issue's own page asks.
--
-- It is also the referencing side of release_issues_issue_fk: without it every
-- `DELETE FROM issues` scans this table in full to satisfy the RESTRICT.
--
-- `release_pull_requests` needs no equivalent. Its only foreign key is
-- release_fk, and `(workspace_id, release_id)` is the leading prefix of its
-- primary key -- so that RESTRICT is already served, and nothing yet asks
-- "which releases contained pull request #84". An index for a query nobody
-- runs is a write cost with no reader.
CREATE INDEX release_issues_workspace_issue_idx
    ON release_issues (workspace_id, issue_id);
