-- The notification pipeline's outbox: what happened, and whether Slack was
-- told about it.
--
-- Until this file, Vector had two ends of an integration and no wire between
-- them. migrations/018_slack_channels.sql stores which channel a workspace
-- posts into and which events it wants announced; app/services/slack.py can
-- post a message. Nothing on any write path connected the two, so the only
-- code in the product that ever called `chat.postMessage` was the test-message
-- button. This table is the wire.
--
-- It is an OUTBOX and not a queue, and the distinction is the whole design.
-- A row here is written by the transaction that performs the change it
-- describes -- the assignment, the merge, the project update -- on that
-- transaction's own connection. So the event and the fact are one commit:
-- there is no window in which Slack has been told about an assignment that was
-- then rolled back, and none in which a committed assignment left no event
-- because a network call failed between them. That is the same argument
-- migrations/012_activity_notifications.sql makes for `issue_activity` being
-- written on the caller's connection, applied to a second reader.
--
-- Three shapes this file exists to make impossible.
--
-- 1. ONE EVENT POSTED TWICE. GitHub redelivers anything it did not answer 2xx
--    for, and it also sends the whole `pull_request` object on every later
--    edit of an already-merged pull request -- so "this merged" is a fact a
--    provider will report to us many times under many delivery ids. The
--    primary key below is (workspace_id, kind, dedupe_key), and the emitter
--    supplies a key naming the FACT rather than the delivery: for a merge it
--    is the repository, the number and the issue, so a second report of the
--    same merge is a key violation rather than a second row -- and therefore
--    never a second message. See the column's own note for what the key means
--    on a path where a fact can honestly recur.
--
-- 2. A WORKSPACE'S EVENT DELIVERED INTO ANOTHER WORKSPACE'S CHANNEL. There is
--    one `workspace_id` on the row and every reference out of it is composite
--    through that column, exactly as migrations/017_github_development.sql
--    argues for its link tables. The channel is not stored here at all: it is
--    read at delivery time from `slack_notification_settings` for THIS row's
--    workspace, and 018's composite foreign key already guarantees that a
--    settings row can only name a channel of its own tenant. So there is no
--    column here through which a channel could be named, and no column in 018
--    through which a foreign channel could have been chosen.
--
-- 3. A DELIVERY REPORTED AS SUCCESSFUL THAT DID NOT HAPPEN. `slack_state =
--    'delivered'` is unspellable without `slack_delivered_at`, and unspellable
--    with a failure reason; `slack_state = 'failed'` is unspellable without
--    one. A service that caught an exception and wrote "delivered" would be
--    refused by the database rather than trusted to be honest -- which is the
--    property `SlackDeliveryResult` documents for the test button, made
--    structural for the path nobody is watching.
--
-- What is deliberately NOT here:
--
--   * a recipient. Slack delivery is to a workspace CHANNEL, and the per-person
--     inbox is `notifications`, which stays the product's source of truth and
--     is written by app/services/activity.py from recipients its own statement
--     derives. Nothing in this table names a person, so nothing about this
--     pipeline is a way for a caller to choose who hears about something.
--   * an actor. Naming who did it would mean joining `users` to render a
--     message and storing a person's identity in an outbox row that outlives
--     their membership. The message says what happened to what, and links to
--     Vector, where the history says who.
--   * a JSONB payload. `subject`, `summary` and `path` are three opaque TEXT
--     columns for the reason 012 gives about `from_value` and `to_value`: a
--     document column accepts a typo forever, is invisible to every query, and
--     cannot be asked which rows a retired field still appears in.
--
-- 018's `slack_notification_preferences_event_known` is NOT widened here. The
-- kinds below are exactly its six, so a preference lookup is `kind = event`
-- with nothing translating between two vocabularies -- and a translation table
-- is precisely where an event silently stops matching the toggle that was
-- meant to control it.
--
-- Apply this ONLY through `python -m scripts.apply_migration
-- migrations/027_domain_events.sql`. The runner wraps the file in one
-- transaction, takes an advisory lock and records a checksum; a hand-run gets
-- none of that and executes statement-at-a-time in autocommit, which here
-- means a table that accepts rows before the CHECK constraints that keep a
-- delivery honest exist.
--
-- DEPENDS ON 002, for `workspaces`; on 007, for `issues_workspace_id_key` --
-- the UNIQUE (workspace_id, id) the issue reference below needs; and on 018,
-- for the event vocabulary this file mirrors. Applying it before any of them
-- fails on that constraint and -- inside the single transaction the runner
-- wraps the file in -- leaves nothing behind. That failure IS the dependency
-- check: the ledger records what has been applied and not what depends on
-- what, so the schema itself is what has to refuse an out-of-order apply.


CREATE TABLE domain_events (
    -- The tenant, and the ONLY tenant this row can ever be about. It feeds the
    -- workspace reference below, the composite issue reference, and -- at
    -- delivery time -- the lookup of which channel this event may be posted
    -- into. One column, read by all three, is what makes "workspace A's event
    -- reaching workspace B's Slack" not a rule a service remembers but a row
    -- that cannot be written.
    workspace_id UUID NOT NULL,

    -- What happened, from the closed set in
    -- `domain_events_kind_known` below.
    --
    -- TEXT with a CHECK rather than a PostgreSQL ENUM, matching
    -- `workflow_states.type` in 005, `notifications.kind` in 012 and
    -- `slack_notification_preferences.event` in 018: adding a value to an ENUM
    -- is a migration that cannot run inside a transaction on older servers,
    -- while a CHECK is an ordinary constraint swap.
    --
    -- Spelled identically to 018's event vocabulary so that deciding whether
    -- to announce an event is a lookup of `(workspace_id, kind)` in
    -- `slack_notification_preferences` and not a mapping some module holds. A
    -- mapping is a third place the six names are written down, and the one
    -- that fails silently -- an event whose toggle nobody can find.
    kind TEXT NOT NULL,

    -- What would make two emissions the SAME event.
    --
    -- Supplied by the emitter and never generated here, because only the
    -- emitter knows what identity means for its source. Two shapes exist and
    -- the difference is worth being exact about:
    --
    --   * a source with at-least-once delivery supplies a NATURAL key. A pull
    --     request merges once, so `pull_request_merged` keys on the
    --     repository, the number and the issue -- and every later delivery
    --     about that merged pull request, under a delivery id this server has
    --     never seen, collides with the row already here. That collision is
    --     the duplicate suppression; without it, editing the description of a
    --     merged pull request would re-announce the merge.
    --
    --   * a source applied exactly once by its own transaction supplies a
    --     fresh key. An issue assigned to Ana, then to Ben, then to Ana again
    --     is three events and not one; there is nothing to suppress, because
    --     the mutation's transaction is what already guarantees each of them
    --     happened once. A key derived from (issue, assignee) would silently
    --     swallow the third.
    --
    -- Bounded, and part of the primary key, so the bound is what stops an
    -- index being made enormous by one long value. 200 matches
    -- `github_deliveries_delivery_id_length` in 017 for the same reason.
    dedupe_key TEXT NOT NULL,

    -- The issue this event is about, for the kinds that have one, checked
    -- against the SAME workspace by the composite reference below.
    --
    -- Nullable because the project kinds have no issue. It is not what the
    -- message is rendered from -- `subject`, `summary` and `path` are, and
    -- they are captured at emit time -- so this column is here for the tenancy
    -- proof rather than for a join: it is what makes an event claiming to be
    -- workspace A's, about workspace B's issue, a row PostgreSQL refuses.
    issue_id UUID,

    -- The three strings a message is built from, captured when the event
    -- happened rather than joined when it is delivered.
    --
    -- Captured, deliberately. A notification describes a moment; re-reading
    -- the issue at delivery time would announce the title as it is now, which
    -- for an event that waited out a Slack outage is a message about something
    -- that did not happen. It also means the delivery path joins nothing,
    -- which is what keeps the connection it holds short -- see
    -- `domain_events_pending_idx`.
    --
    --   subject  -- what it is about, short: "ENG-142", or a project's name.
    --   summary  -- one line: the issue's title, the merged pull request's
    --              title, the health a project moved to.
    --   path     -- the Vector route, workspace-relative and absolute from the
    --              site root: "/acme/issues/<uuid>". A PATH and not a whole
    --              URL, because the origin is a property of the deployment
    --              (and of which hostname an operator serves it on today)
    --              rather than of the event -- storing it would bake a
    --              hostname into rows that outlive it, and every stored
    --              message would point at the old one after a move.
    --
    -- Every one of them is composed from values this schema already
    -- constrains -- a workspace slug, a team key, an issue number, a uuid --
    -- so `path` needs no escaping to be a safe URL. That is a property of
    -- where they come from and not of this column, which is why the emitting
    -- statements build it from columns rather than from anything a client
    -- sent.
    subject TEXT NOT NULL,
    summary TEXT NOT NULL,
    path TEXT NOT NULL,

    -- When the thing happened, which is not when it was delivered.
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- --- the Slack adapter's own state ---------------------------------
    --
    -- Five columns on this row rather than a `slack_deliveries` table beside
    -- it, and the prefix is the seam: they describe what ONE adapter did with
    -- this event, not what the event is. Vector has exactly one delivery
    -- adapter today, so a second table would be a join and a second insert per
    -- event to express a one-to-one relationship. A second adapter -- email,
    -- a webhook -- gets its own table keyed on this row rather than a second
    -- prefix here, and nothing above this line has to change for it: the event
    -- half of this table says nothing about Slack.

    -- Where this event's Slack delivery has got to.
    --
    --   pending    -- not yet delivered, and due at `slack_next_attempt_at`.
    --   delivered  -- Slack answered `ok: true`. Nothing else reaches here.
    --   failed     -- it will not be retried, and `slack_failure` says why.
    --   skipped    -- deliberately not sent: the workspace has this event
    --                 switched off, has not connected Slack, or has chosen no
    --                 channel. A terminal state and NOT a failure, because
    --                 nothing went wrong and nobody has anything to fix.
    --
    -- `skipped` is a separate word from `failed` on purpose. Collapsing them
    -- would make a settings screen count every event a workspace deliberately
    -- disabled as an integration error, which is how a healthy integration
    -- comes to look broken.
    slack_state TEXT NOT NULL DEFAULT 'pending',

    -- Why, in the vocabulary `SLACK_FAILURES` already defines in
    -- app/domain/slack.py, plus the two states only this pipeline can be in.
    --
    -- Our words and never Slack's. Slack's `error` field is a third party's
    -- identifier set that changes without notice, and echoing it would put a
    -- provider's vocabulary into this schema and into every screen that reads
    -- it. app/services/slack.py's `_failure_for` is the one frame that knows
    -- the difference, and it is what fills this in.
    slack_failure TEXT,

    -- How many times delivery has been attempted, incremented when an attempt
    -- is CLAIMED rather than when it finishes.
    --
    -- Claiming is what makes the counter honest across a crash: a process that
    -- died between posting to Slack and recording the outcome has already
    -- spent an attempt, and a counter bumped on completion would not know it.
    -- The alternative -- an unbounded retry of a message that will never be
    -- accepted -- is a channel receiving the same failure forever.
    slack_attempts INTEGER NOT NULL DEFAULT 0,

    -- The earliest moment a delivery may be attempted. Defaults to now, so a
    -- freshly written event is immediately due.
    --
    -- This column is what makes a retry unable to hot-loop, and it does it
    -- structurally rather than by a sleep somewhere in a worker: claiming an
    -- attempt moves this forward in the SAME statement that increments
    -- `slack_attempts`, so a failing event is not due again until the backoff
    -- has passed -- whatever the loop above it does, however many processes
    -- are running it, and even if one of them crashes mid-attempt.
    slack_next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- When Slack acknowledged it. The paired CHECK below is what makes this
    -- inseparable from `slack_state = 'delivered'`.
    slack_delivered_at TIMESTAMPTZ,

    -- The row IS its key: one tenant, one kind, one fact. No surrogate id
    -- beside it, for the reason `github_pull_requests` gives in 017 -- a
    -- second way to address one row is how a redelivery becomes a second row
    -- -- and because the key is also the index the emitting upsert conflicts
    -- on.
    --
    -- `kind` is INSIDE the key, so one merged pull request that closes an
    -- issue and one assignment of the same issue are two events. Only two
    -- emissions of the same KIND about the same fact are the same event.
    CONSTRAINT domain_events_pkey PRIMARY KEY (workspace_id, kind, dedupe_key),

    CONSTRAINT domain_events_workspace_fk
        FOREIGN KEY (workspace_id)
        REFERENCES workspaces (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    -- Composite through `workspace_id`, so the issue is checked against the
    -- SAME tenant that owns the event. `REFERENCES issues (id)` is the obvious
    -- spelling and it is the bug: it would check that the id names an issue
    -- somewhere and say nothing about whose, which is how a delivery about
    -- another tenant's issue becomes storable and then postable.
    --
    -- MATCH SIMPLE, the default, which is what makes the nullable case work:
    -- with `issue_id` NULL the constraint is not checked at all, so the
    -- project kinds need no sentinel.
    --
    -- RESTRICT rather than CASCADE, in line with the rest of this schema. An
    -- issue that goes must not silently take with it the record of what was
    -- announced about it -- and Vector archives issues rather than deleting
    -- them, so this constraint fires for a hard delete somebody is performing
    -- deliberately and should be made to think about.
    CONSTRAINT domain_events_issue_fk
        FOREIGN KEY (workspace_id, issue_id)
        REFERENCES issues (workspace_id, id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    -- The vocabulary, and exactly 018's six. See the note on `kind`.
    CONSTRAINT domain_events_kind_known
        CHECK (
            kind IN (
                'issue_assigned',
                'issue_completed',
                'issue_priority_urgent',
                'project_health_changed',
                'project_update_published',
                'pull_request_merged'
            )
        ),

    CONSTRAINT domain_events_dedupe_key_length
        CHECK (length(dedupe_key) BETWEEN 1 AND 200),

    -- Bounds rather than validations. Slack renders a long message by
    -- truncating it, so the ceiling that matters is the one that keeps a row
    -- -- and the index over the key beside it -- from being made enormous by a
    -- long title. The emitting statements truncate to these values, so a
    -- legitimate 4,000-character issue title produces a short message rather
    -- than a failed delivery.
    CONSTRAINT domain_events_subject_length CHECK (length(subject) BETWEEN 1 AND 200),
    CONSTRAINT domain_events_summary_length CHECK (length(summary) BETWEEN 1 AND 300),

    -- A path and not a URL: it starts at the site root and carries no scheme
    -- and no host. A stored absolute URL would be a deployment's hostname
    -- baked into rows that outlive it. Printable ASCII with no spaces, which
    -- is what a value built from a slug, a team key, a number and a uuid
    -- produces -- so a path that fails this is a path something built out of
    -- text it should not have.
    --
    -- The bound is a separate constraint rather than a repetition count in the
    -- pattern, and that is forced rather than stylistic: PostgreSQL's regular
    -- expressions cap a `{m,n}` count at 255, so `{0,511}` is not a tighter
    -- rule -- it is a syntax error that takes the whole migration down.
    CONSTRAINT domain_events_path_shape
        CHECK (path ~ '^/[!-~]*$'),

    CONSTRAINT domain_events_path_length CHECK (length(path) BETWEEN 1 AND 512),

    CONSTRAINT domain_events_slack_state_known
        CHECK (slack_state IN ('pending', 'delivered', 'failed', 'skipped')),

    -- The constraint the file's third paragraph is about, and the reason it is
    -- written as an equality between two tests rather than as two
    -- implications: there is one expression to get right, and it closes both
    -- directions at once. `delivered` without an instant is a claim nothing
    -- backs; an instant without `delivered` is a delivery the state does not
    -- admit to.
    CONSTRAINT domain_events_delivered_has_an_instant
        CHECK ((slack_state = 'delivered') = (slack_delivered_at IS NOT NULL)),

    -- `failed` must say why. A failure with no reason is an operator staring
    -- at a red row with nothing to act on, and it is exactly what a service
    -- that swallowed an exception would write.
    CONSTRAINT domain_events_failed_has_a_reason
        CHECK (slack_state <> 'failed' OR slack_failure IS NOT NULL),

    -- And the two states that are not an outcome must say nothing. A
    -- `delivered` row carrying a failure reason is a row two readers would
    -- disagree about; a `pending` one carrying a stale reason from a previous
    -- attempt is a row that reports a failure it has since recovered from.
    CONSTRAINT domain_events_progress_has_no_reason
        CHECK (
            slack_state NOT IN ('pending', 'delivered')
            OR slack_failure IS NULL
        ),

    -- The reasons this pipeline can record. Six are `SLACK_FAILURES` in
    -- app/domain/slack.py, unchanged; two exist only here because they are
    -- decisions this pipeline makes rather than answers Slack gave:
    --
    --   preference_disabled -- the workspace has this event switched off.
    --                          Absence of a preference row means off, which is
    --                          018's opt-in rule, so this is by far the
    --                          commonest reason a `skipped` row exists.
    --   no_installation     -- distinct from `not_connected`, which is what
    --                          Slack says when a token it once issued has
    --                          stopped working. This one means nobody ever
    --                          connected Slack here, and the difference is the
    --                          difference between "reconnect" and "there is
    --                          nothing wrong".
    CONSTRAINT domain_events_slack_failure_known
        CHECK (
            slack_failure IS NULL
            OR slack_failure IN (
                'not_connected',
                'missing_scope',
                'no_default_channel',
                'channel_unavailable',
                'slack_refused',
                'slack_unreachable',
                'preference_disabled',
                'no_installation'
            )
        ),

    CONSTRAINT domain_events_attempts_not_negative CHECK (slack_attempts >= 0)
);


-- The only query the delivery loop runs: "what is due".
--
-- Partial, on the state the loop looks for, which is what keeps its size
-- proportional to the BACKLOG rather than to every event the product has ever
-- emitted. A `delivered` row is dead weight to this index the moment it lands,
-- and a full index would go on paying for it on every insert forever -- the
-- same argument `notifications_workspace_user_unread_idx` in 012 makes for the
-- unread badge.
--
-- Not workspace-scoped, and deliberately: the loop drains every tenant's
-- backlog and has no workspace in hand to narrow by. Whose an event is gets
-- answered by the row it claims, and everything the delivery then reads --
-- the preference, the channel, the token -- is looked up under THAT
-- workspace_id.
--
-- ponytail: no pruning job ships with this migration, so a delivered row lives
-- forever. It costs nothing to this index (which excludes it) and a delivery
-- history is worth having; the sweep belongs with whatever scheduler
-- eventually prunes `github_deliveries`, which 017 says the same thing about.
CREATE INDEX domain_events_pending_idx
    ON domain_events (slack_next_attempt_at)
    WHERE slack_state = 'pending';

-- The referencing side of domain_events_issue_fk.
--
-- PostgreSQL indexes the REFERENCED side of a foreign key and never the
-- referencing side, so without this every `DELETE FROM issues` scans this
-- whole table to satisfy the RESTRICT. The same index 017 creates for both of
-- its link tables, for the same reason.
--
-- `workspace_id` leads it, which also serves the RESTRICT check on
-- domain_events_workspace_fk -- though the primary key's leading column
-- already does that, so this index is here for the issue reference alone.
CREATE INDEX domain_events_workspace_issue_idx
    ON domain_events (workspace_id, issue_id);
