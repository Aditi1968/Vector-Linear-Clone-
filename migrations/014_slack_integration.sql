-- Slack: the link between one Vector workspace and one Slack workspace, and
-- the ledger that makes Slack's retries harmless.
--
-- Two tables, and they are here together because neither is useful alone. The
-- installation is what an incoming event is routed by; the delivery ledger is
-- what stops that routing happening three times for one message.
--
-- The shape this file exists to make impossible is a second installation row
-- for the same Slack workspace. An event arrives carrying Slack's `team_id`
-- and nothing else -- no Vector workspace, no slug, no session -- so that
-- column IS the routing key, and two rows sharing it would mean the server
-- picks whichever the planner reached first and delivers one tenant's Slack
-- traffic into another tenant's data. `slack_installations_team_key` below is
-- what turns that from a rule someone has to remember into a row PostgreSQL
-- will not store.
--
-- Apply this ONLY through `python -m scripts.apply_migration
-- migrations/014_slack_integration.sql`. The runner wraps the file in one
-- transaction, takes an advisory lock and records a checksum; a hand-run gets
-- none of that and executes statement-at-a-time in autocommit, which here
-- means a `slack_installations` that exists while the delivery ledger does
-- not -- an integration that accepts events and deduplicates none of them.
--
-- DEPENDS ON 002, which creates `workspaces`, and on 004, which creates
-- `workspace_members`. `slack_installations_connected_by_fk` references that
-- table's primary key, so applying this file before 004 fails on that
-- constraint and -- inside the single transaction the runner wraps the file
-- in -- leaves nothing behind. That failure IS the dependency check: the
-- ledger records what has been applied and not what depends on what, so the
-- schema itself is what has to refuse an out-of-order apply.


CREATE TABLE slack_installations (
    -- The primary key, and deliberately the whole key. A workspace has one
    -- Slack connection or none, so the workspace IS the identity of the row;
    -- a surrogate `id` beside it would need a UNIQUE (workspace_id) anyway to
    -- say the same thing, and would leave two ways to address one row.
    --
    -- Reconnecting therefore overwrites rather than accumulating. That is the
    -- honest model: a second OAuth grant for the same workspace replaces the
    -- first, because the bot token the first one carried has been superseded
    -- and keeping it would leave a live credential nothing in the product can
    -- reach or revoke.
    workspace_id UUID PRIMARY KEY,

    -- Slack's identifiers, as Slack spells them: "T024BE7LD", "U0G9QF9C6".
    -- TEXT and not UUID, because they are neither; and stored rather than
    -- derived because every inbound event names the team and nothing else.
    slack_team_id TEXT NOT NULL,

    -- Cached at connection time and never refreshed here. A workspace that
    -- renames itself in Slack will read stale in Vector until someone
    -- reconnects, and that is the right trade for a label: the alternative is
    -- an API call on the path of every status read, to display a name.
    slack_team_name TEXT NOT NULL,

    -- The bot's own user id, and the one column here with a security job.
    --
    -- Vector posts as this user. Slack then delivers Vector's own message
    -- back to Vector as an event, so without this id the server cannot tell
    -- an event it caused from an event a person caused -- and an integration
    -- that reacts to its own output is a loop that runs at Slack's rate limit
    -- until someone notices. app/rest/slack.py compares against this column;
    -- see the comment there for the two other signals it checks alongside it.
    bot_user_id TEXT NOT NULL,

    -- What the grant actually permits, as Slack returned it.
    --
    -- TEXT[] rather than the comma-joined string Slack sends, because the
    -- product question is always "is this scope present" and answering it
    -- against a string means substring matching -- where `channels:read`
    -- matches inside `channels:read:history` and reports a permission the
    -- workspace never granted.
    --
    -- Stored rather than assumed: what Vector asks for and what an admin
    -- approves are different lists, a Slack admin can decline individual
    -- scopes, and a feature gated on a scope has to read what was granted
    -- rather than what this release happens to request.
    scopes TEXT[] NOT NULL,

    -- The bot token, behind one level of indirection.
    --
    -- `bot_token_backend` names which token store produced the reference, and
    -- `bot_token_reference` is whatever that store handed back. Today the
    -- only store is 'database' and the reference IS the token, in this
    -- column, in plaintext.
    --
    -- What that gives, exactly: nothing beyond PostgreSQL's own access
    -- control and whatever the storage layer encrypts underneath it. Anyone
    -- who can read this table -- a backup, a restored snapshot, a support
    -- query, a SELECT * in a log -- holds a live credential that can post as
    -- Vector into every channel the bot is in, and the only revocation is
    -- Slack's. It is NOT hashed, and cannot be: unlike a session token or an
    -- invitation token, this is a secret the server has to present to a third
    -- party, so a one-way digest would leave nothing to send. Contrast
    -- `workspace_invitations.token_hash` in 004, which stores a digest
    -- precisely because nobody ever has to replay it.
    --
    -- What the indirection buys is that fixing this is a swap and not a
    -- schema change. A KMS or secret-manager store writes 'kms' here and an
    -- ARN or key id in the reference; the columns, the constraints and every
    -- statement in app/repositories/slack.py stay as they are, and rows
    -- written by the old store keep working because the row says which store
    -- wrote it. See app/services/slack.py::SlackTokenStore.
    --
    -- Deliberately NO CHECK on the vocabulary, which is where this column
    -- departs from `workspace_members.role`. A CHECK listing the known stores
    -- would mean adding a store requires a migration -- which is exactly the
    -- coupling the column exists to remove. The set of stores is the
    -- application's, and the application is what refuses one it cannot read.
    bot_token_backend TEXT NOT NULL,
    bot_token_reference TEXT NOT NULL,

    -- Who authorized this, and when. An OAuth grant is an act by a person,
    -- and "which admin connected our Slack, and when" is the first question
    -- asked of an integration nobody remembers installing.
    --
    -- NOT NULL, unlike `projects.lead_id`: there is no such thing as an
    -- installation nobody performed. The flow that writes this row runs only
    -- for an authenticated admin of the workspace, so an absent value would
    -- mean the row was written by something that skipped that check.
    connected_by_user_id UUID NOT NULL,

    connected_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- RESTRICT on both sides, matching teams_workspace_fk in 002. A workspace
    -- with a live Slack connection is not something to delete by accident,
    -- and a cascade here would drop a bot token with no statement anywhere
    -- naming this table -- leaving a credential live at Slack that Vector no
    -- longer knows it holds.
    CONSTRAINT slack_installations_workspace_fk
        FOREIGN KEY (workspace_id)
        REFERENCES workspaces (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    -- The connector must be a member of THIS workspace.
    --
    -- `REFERENCES users (id)` is the obvious spelling and it is the bug, for
    -- the reason projects_lead_fk gives at length in 009: it checks that the
    -- id names a real account somewhere and says nothing about where, so any
    -- user from any tenant would be recordable as the admin who connected any
    -- workspace's Slack. The reference is composite instead, onto
    -- `workspace_members (workspace_id, user_id)` -- that table's PRIMARY KEY,
    -- marked [FK TARGET] in 004 for exactly this. One workspace_id column
    -- feeds both this constraint and the one above, so there is no column
    -- left for a second tenant to go in.
    --
    -- Both columns are NOT NULL, so MATCH SIMPLE never skips this check.
    --
    -- ON DELETE RESTRICT, like every foreign key in this schema: removing the
    -- admin who connected Slack is refused rather than silently rewriting who
    -- the audit trail says did it. The caller disconnects the integration
    -- first, which is a decision someone should be making out loud.
    CONSTRAINT slack_installations_connected_by_fk
        FOREIGN KEY (workspace_id, connected_by_user_id)
        REFERENCES workspace_members (workspace_id, user_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    -- One Slack workspace connects to at most one Vector workspace.
    --
    -- This is the routing guarantee stated at the top of the file, and it is
    -- also the index the events endpoint reads: every event lookup is an
    -- equality on `slack_team_id`, which a UNIQUE constraint already indexes,
    -- so no second index is declared for it.
    --
    -- The product cost is real and accepted: a company running two Vector
    -- workspaces cannot point both at one Slack. The alternative is an
    -- ambiguous route with no tie-break that is not a guess, and a guess here
    -- delivers messages across a tenant boundary.
    CONSTRAINT slack_installations_team_key UNIQUE (slack_team_id)
);

-- No index on (workspace_id, connected_by_user_id), and the contrast with
-- projects_workspace_lead_idx in 009 is deliberate rather than an omission.
-- PostgreSQL indexes the referenced side of a foreign key and never the
-- referencing side, so a deletion from `workspace_members` has to scan this
-- table -- but this table holds at most ONE row per workspace, and the
-- primary key on `workspace_id` narrows that scan to that row before the
-- second column is compared. 009 needs its index because a workspace has many
-- projects and the leading column alone does not narrow anything.


-- Every Slack event this server has already accepted.
--
-- Not optional, and not a cache. Slack retries a delivery three times on any
-- non-2xx and again on a timeout, and it re-sends during its own incidents;
-- an endpoint that creates an issue from a message and is not deduplicated
-- creates that issue up to four times. The retry is Slack's contract, so
-- exactly-once has to be built on this side of the socket.
--
-- The row IS the record: its presence means "seen", so there is no status
-- column that can come to disagree with it. Insert-and-see-if-it-landed is
-- therefore the whole protocol -- see SlackRepository.record_event, which
-- does it in one statement rather than SELECT-then-INSERT, because the gap
-- between those two is precisely where a retry that arrives while the first
-- delivery is still being processed slips through.
CREATE TABLE slack_event_deliveries (
    -- Slack's own `event_id` ("Ev08MFMKH6"), which is unique across all of
    -- Slack and stable across the retries of one delivery -- that stability
    -- is the entire reason this table can work.
    --
    -- The primary key, so uniqueness is enforced by the same object the
    -- lookup reads. No workspace_id and no `slack_team_id`: an event id is
    -- already globally unique, so scoping it would only weaken the key, and
    -- there is nothing tenant-owned in a row that holds no content.
    event_id TEXT PRIMARY KEY,

    received_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- This table grows without bound and nothing here prunes it.
--
-- Pruning is a scheduled DELETE, and a migration is not a scheduler; writing
-- one here would also put a destructive statement in a file that must not
-- contain one. What the migration can do is make the eventual prune cheap,
-- which is what this index is: `DELETE FROM slack_event_deliveries WHERE
-- received_at < now() - interval '...'` reads it instead of scanning.
--
-- The retention window is a policy decision and belongs with the job that
-- enforces it, not in this file. It only has to exceed the longest interval
-- over which Slack will re-send one event id.
CREATE INDEX slack_event_deliveries_received_at_idx
    ON slack_event_deliveries (received_at);
