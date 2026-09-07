-- Slack, after the connect button: which channels a workspace can reach, which
-- one it posts into, and which events are worth posting at all.
--
-- migrations/014_slack_integration.sql stores the grant. It stores nothing a
-- product can act on: an installation row plus a delivery ledger is an
-- integration that can authenticate and has nowhere to speak. This file is the
-- other half -- the three facts that turn a stored bot token into a feature an
-- admin configures.
--
-- The shape this file exists to make impossible is one workspace naming
-- another workspace's channel.
--
-- A channel id is a short opaque string ("C024BE91L") that arrives from a
-- browser, chosen by an admin from a picker -- which means it arrives from
-- whoever controls that browser, in whatever form they like. An admin of
-- workspace A who submits workspace B's channel id must not be able to store
-- it, and must not be able to learn from the answer whether that channel
-- exists. So `slack_notification_settings.default_channel_id` is not a bare
-- TEXT column that a service is trusted to have checked: it is half of a
-- composite foreign key onto `slack_channels (workspace_id, channel_id)`, read
-- through the SAME workspace_id that identifies the settings row. There is no
-- column left for a second tenant to go in, exactly as
-- migrations/017_github_development.sql argues for its link tables.
--
-- The second thing it makes impossible is a preference stored as a blob.
-- `slack_notification_preferences` is one row per (workspace, event), with the
-- vocabulary in a CHECK. A JSONB column would accept `{"issue_asigned": true}`
-- -- a typo that silently disables a notification forever, is invisible to
-- every query, and cannot be found by asking the database which workspaces
-- have a setting for an event that is being retired.
--
-- Apply this ONLY through `python -m scripts.apply_migration
-- migrations/018_slack_channels.sql`. The runner wraps the file in one
-- transaction, takes an advisory lock and records a checksum; a hand-run gets
-- none of that and executes statement-at-a-time in autocommit, which here
-- means a `slack_notification_settings` whose channel foreign key does not
-- exist yet -- a table that accepts precisely the cross-tenant row the file
-- was written to refuse.
--
-- DEPENDS ON 014, for `slack_installations`, whose primary key every table
-- below hangs from. Applying this file first fails on that constraint and --
-- inside the single transaction the runner wraps the file in -- leaves nothing
-- behind. That failure IS the dependency check: the ledger records what has
-- been applied and not what depends on what, so the schema itself is what has
-- to refuse an out-of-order apply.


-- The channels this workspace's bot token can see, as `conversations.list`
-- last reported them.
--
-- A cache with a job, not a mirror. Three things have to be true of it and
-- none of them is "identical to Slack right now":
--
-- 1. The id is what is stored and the name is what is shown. Slack channel
--    names are renamed casually and the id never changes, so a settings row
--    that recorded "#engineering" would silently start pointing at whatever
--    channel later took that name -- or at nothing. Every reference in this
--    schema is by id.
-- 2. A channel that stops being listed is MARKED, never deleted. See
--    `is_accessible`.
-- 3. Reading the picker must not call Slack. A settings page that hit
--    `conversations.list` on every render would burn the workspace's rate
--    limit on people looking at a page, and would fail to render at all
--    whenever Slack is slow. The sync is an explicit act; this table is what
--    it leaves behind.
CREATE TABLE slack_channels (
    -- The tenant, and the ONLY tenant this row can ever be about. It feeds the
    -- installation key below and -- through the composite key in
    -- `slack_notification_settings` -- the choice that points back here.
    workspace_id UUID NOT NULL,

    -- Slack's own id, as Slack spells it: "C024BE91L". TEXT and not UUID
    -- because it is neither, and stored rather than derived because it is the
    -- only stable handle Slack offers. `chat.postMessage` takes it directly,
    -- so nothing has to resolve a name at post time -- which matters, because
    -- resolving a name at post time is how a message reaches the wrong room.
    channel_id TEXT NOT NULL,

    -- Denormalised for display, and refreshed by the sync rather than by a
    -- join. The alternative -- store only the id and look the name up when
    -- rendering -- reads better until the channel is one this workspace can no
    -- longer see, at which point the settings screen can show an admin an
    -- opaque id and nothing else. A name that is a few minutes stale is a
    -- better answer than no name.
    --
    -- Without the leading '#'. That is presentation, and a stored '#' is a
    -- character every comparison then has to remember to strip.
    name TEXT NOT NULL,

    -- Slack's own flags, stored as Slack sends them rather than collapsed into
    -- one "usable" boolean. The four below answer different questions and an
    -- admin needs different words for each:
    --
    --   is_private     -- a private channel. Vector's granted scopes cover
    --                     public channels only (`channels:read`), so this is
    --                     FALSE for every row a `conversations.list` with
    --                     `types=public_channel` produces. The column exists
    --                     so that a later grant of `groups:read` is a code
    --                     change and not a migration, and so that a row whose
    --                     provenance changes is still readable.
    --   is_archived    -- archived in Slack. Still listed, still has an id,
    --                     and posting to it fails. A picker shows it greyed
    --                     rather than hiding it, because an admin who chose it
    --                     last month needs to see WHY notifications stopped.
    --   is_member      -- the bot is in the channel. This is the one that
    --                     decides whether a post will work: with `chat:write`
    --                     alone, `chat.postMessage` into a public channel the
    --                     bot has not joined is refused with `not_in_channel`.
    --                     The fix is an admin typing `/invite`, which is a
    --                     sentence the product can only say if it stores this.
    --   is_accessible  -- the last sync listed it. See below.
    is_private BOOLEAN NOT NULL DEFAULT FALSE,
    is_archived BOOLEAN NOT NULL DEFAULT FALSE,
    is_member BOOLEAN NOT NULL DEFAULT FALSE,

    -- Whether the most recent sync still found this channel.
    --
    -- This column is why the sync marks instead of deleting, and the reason is
    -- the foreign key in `slack_notification_settings`: it is RESTRICT, so a
    -- sync that deleted a row somebody had chosen as their default channel
    -- would abort the whole sync -- or, with CASCADE, would silently discard
    -- the choice and leave an admin with notifications that stopped for no
    -- stated reason.
    --
    -- A channel drops out of `conversations.list` for several ordinary
    -- reasons: it was deleted, it was converted to private, or the bot was
    -- removed from the workspace's view of it. None of them is a reason to
    -- forget the name; all of them are a reason to stop offering it in a
    -- picker and to tell an admin that the channel they picked is gone. FALSE
    -- says exactly that and keeps the row to say it with.
    is_accessible BOOLEAN NOT NULL DEFAULT TRUE,

    -- When the sync last saw this row. An operator question ("is this list
    -- from today or from March?") rather than a product one, and the value a
    -- future staleness banner would read.
    synced_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- [FK TARGET] What slack_notification_settings_default_channel_fk
    -- references, and the key the sync's upsert conflicts on.
    --
    -- No second UNIQUE over the same columns beside it. A foreign key may
    -- reference any UNIQUE column set and a PRIMARY KEY is one, so the extra
    -- constraint would buy nothing but a second B-tree holding the same
    -- entries and paying the same write cost on every sync. The marker is here
    -- so a reader looking for what points at this table finds it without
    -- grepping.
    CONSTRAINT slack_channels_pkey PRIMARY KEY (workspace_id, channel_id),

    -- A channel belongs to an INSTALLATION, not merely to a workspace.
    --
    -- Referencing `slack_installations` rather than `workspaces` is what makes
    -- "channels this workspace's bot can see" a statement the schema can
    -- enforce: without an installation there is no bot token, so there is
    -- nothing that could have listed a channel, and a row here would be a
    -- claim about an API call that never happened. `slack_installations`
    -- keys on `workspace_id` alone (it is that table's whole primary key --
    -- one workspace has one Slack connection or none), so this single-column
    -- reference is still a tenancy check: the column it constrains IS the
    -- tenant.
    --
    -- RESTRICT on both sides, like every foreign key in this schema.
    -- Disconnecting must not silently discard a workspace's channel list and
    -- notification choices while the command tag reads `DELETE 1` --
    -- reconnecting five minutes later would then present an admin with an
    -- empty picker and no explanation. SlackService.disconnect removes these
    -- rows itself, in order, in the same transaction, which is a decision made
    -- out loud in code rather than a side effect of a cascade.
    CONSTRAINT slack_channels_installation_fk
        FOREIGN KEY (workspace_id)
        REFERENCES slack_installations (workspace_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    -- A bound and a shape, not a claim that the channel exists -- only Slack
    -- can say that, and it says it by returning the id from an authenticated
    -- call. What this refuses is the empty string (which would be an absent
    -- key read as ''), and an unbounded value used as half of a primary key,
    -- which is how an index is made enormous by one crafted response.
    --
    -- Slack conversation ids are uppercase alphanumeric and around nine to
    -- eleven characters; 64 is generously above anything observed, because a
    -- check that is too tight here fails a real workspace's real channel and
    -- can only be relaxed by another migration.
    CONSTRAINT slack_channels_channel_id_format
        CHECK (channel_id ~ '^[A-Z0-9]{2,64}$'),

    -- Slack's own ceiling for a channel name is 80 characters. Matching it
    -- means a name Slack accepts is a name this table accepts, so a sync
    -- cannot fail on a channel somebody legitimately created.
    CONSTRAINT slack_channels_name_length
        CHECK (length(name) BETWEEN 1 AND 80)
);

-- No index beyond the primary key, and that is a decision rather than an
-- omission.
--
-- The referencing side of slack_channels_installation_fk is `workspace_id`,
-- which is the leading column of the primary key -- so the RESTRICT check a
-- `DELETE FROM slack_installations` performs already has an index to read, and
-- a second one on the same column would be redundant.
--
-- The other query this table faces is the picker: every channel of one
-- workspace, ordered by name. The primary key narrows that to one workspace
-- and PostgreSQL sorts what is left. An index on (workspace_id, name) would
-- turn the sort into a scan, and would be paid for on every row of every sync
-- -- which is the write-heavy path here. That trade only pays off for a
-- workspace with enough channels for the sort to be measurable, and no such
-- workspace exists yet. An index for a query nobody has complained about is a
-- write cost with no reader.


-- Where this workspace's Slack notifications go.
--
-- A table of its own rather than two more columns on `slack_installations`,
-- and the reason is the foreign key rather than tidiness. A default channel
-- column on the installations table would have to reference `slack_channels`,
-- which references `slack_installations` -- a cycle, which is storable but
-- makes every disconnect a three-statement dance (null the choice, delete the
-- channels, delete the installation) and makes the order of those statements a
-- thing to remember rather than a thing to read. Split, the dependency runs
-- one way and the teardown is a list.
CREATE TABLE slack_notification_settings (
    -- The primary key, and deliberately the whole key: a workspace has one
    -- default channel or none. A surrogate id beside it would need a UNIQUE
    -- (workspace_id) anyway to say the same thing, and would leave two ways to
    -- address one row -- the argument `slack_installations` makes in 014,
    -- applied to the same shape.
    workspace_id UUID PRIMARY KEY,

    -- The channel, by id, or NULL for a workspace that has not chosen one.
    --
    -- Nullable and NOT defaulted, because "no channel yet" is a real state
    -- with its own screen: an integration that is connected but has nowhere to
    -- post is exactly what an admin sees between pressing connect and
    -- finishing setup, and inventing a channel for them would mean posting
    -- into a room nobody agreed to.
    default_channel_id TEXT,

    -- The name as it read when the choice was made, refreshed by every sync
    -- that finds the channel renamed.
    --
    -- Denormalised on purpose, and the duplication is the point: a settings
    -- screen has to be able to say "#engineering" for a channel that has since
    -- become inaccessible, and a join to `slack_channels` answers that only
    -- for as long as the row is still reachable. The refresh is one UPDATE at
    -- the end of a sync -- see SlackRepository.replace_channels -- so a rename
    -- in Slack shows up in Vector at the next sync rather than never.
    default_channel_name TEXT,

    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT slack_notification_settings_installation_fk
        FOREIGN KEY (workspace_id)
        REFERENCES slack_installations (workspace_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    -- The constraint the whole file is about.
    --
    -- Composite through `workspace_id`, so the channel is checked against the
    -- SAME tenant that owns the settings row rather than against a second one
    -- spelled separately. `REFERENCES slack_channels (channel_id)` is the
    -- obvious spelling and it is the bug -- it would check that the id names a
    -- channel somewhere and say nothing about whose, so an admin of workspace
    -- A could store workspace B's channel id and, if a delivery ever ran,
    -- deliver A's issue titles into B's Slack.
    --
    -- MATCH SIMPLE, which is the default and is what makes the nullable case
    -- work: with any column of the key NULL the constraint is not checked at
    -- all, so "no channel chosen" needs no sentinel row. The paired CHECK
    -- below is what stops that leniency from also admitting half a choice.
    --
    -- ON DELETE RESTRICT, so a channel a workspace has chosen cannot be
    -- removed from the cache underneath them. That is why the sync marks
    -- `is_accessible = FALSE` instead of deleting: this constraint is what
    -- would otherwise abort a routine sync.
    CONSTRAINT slack_notification_settings_default_channel_fk
        FOREIGN KEY (workspace_id, default_channel_id)
        REFERENCES slack_channels (workspace_id, channel_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    -- The two columns are one fact and must agree about whether it is set.
    --
    -- Without this, a partial write leaves an id with no name (a settings
    -- screen showing a raw "C024BE91L") or -- far worse -- a name with no id,
    -- which reads to a human as a configured integration and posts nowhere.
    -- Written as an equality between two IS NULL tests rather than as two
    -- implications, so there is one expression to get right.
    CONSTRAINT slack_notification_settings_channel_pair
        CHECK ((default_channel_id IS NULL) = (default_channel_name IS NULL)),

    CONSTRAINT slack_notification_settings_channel_name_length
        CHECK (
            default_channel_name IS NULL
            OR length(default_channel_name) BETWEEN 1 AND 80
        )
);

-- No index on (workspace_id, default_channel_id), and the contrast with
-- migrations/017_github_development.sql's link tables is deliberate. Deleting
-- from `slack_channels` has to scan this table to satisfy the RESTRICT -- but
-- this table holds at most ONE row per workspace, and the primary key on
-- `workspace_id` narrows that scan to that row before the second column is
-- compared. 017 needs its indexes because a workspace has many issues and the
-- leading column alone narrows nothing. The same argument 014 makes for
-- `slack_installations_connected_by_fk`.


-- Which events this workspace wants announced in Slack.
--
-- Rows, one per (workspace, event), and not a JSONB column. The difference is
-- not style:
--
--   * a CHECK can enumerate the events, so `issue_asigned` is refused at write
--     time instead of becoming a key that silently matches nothing forever;
--   * "which workspaces still enable the event we are retiring" is a SELECT
--     rather than a JSON scan nobody will write;
--   * two admins toggling two different events do not overwrite each other,
--     which a read-modify-write of one document does.
--
-- Six events, chosen because each one is something a person would act on
-- within the hour. Everything else that happens to an issue belongs in its
-- history -- the same argument `NotificationKind` in app/domain/notifications.py
-- makes for keeping the inbox vocabulary far shorter than the activity one. A
-- Slack channel that announces every field edit is a channel muted within a
-- week, which is the same as no integration, arrived at expensively.
CREATE TABLE slack_notification_preferences (
    workspace_id UUID NOT NULL,

    -- The event, from the closed set below. TEXT with a CHECK rather than a
    -- PostgreSQL ENUM, matching `workflow_states.type` in 005 and
    -- `notifications.kind` in 012: adding a value to an ENUM is a migration
    -- that cannot run inside a transaction on older servers, while a CHECK is
    -- an ordinary constraint swap. The application's copy of this vocabulary
    -- is SLACK_NOTIFICATION_EVENTS in app/domain/slack.py, and a test pins the
    -- two equal.
    event TEXT NOT NULL,

    -- Explicit, and NOT defaulted.
    --
    -- The absence of a row means the event is off. That is the product
    -- decision -- posting into a company's Slack is a visible act, so it is
    -- opt-in per event rather than something that starts happening the moment
    -- a channel is picked -- and this column exists so that turning something
    -- OFF again is a stored fact rather than a deletion. A toggle implemented
    -- as insert-or-delete cannot tell "never configured" from "deliberately
    -- disabled", which is the difference between a default a future release
    -- may change and a choice it must not.
    enabled BOOLEAN NOT NULL,

    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- The key is also the uniqueness rule ("one setting per event per
    -- workspace") and also the index every read uses. `workspace_id` leads, so
    -- it additionally serves the RESTRICT check on the installation key below.
    CONSTRAINT slack_notification_preferences_pkey
        PRIMARY KEY (workspace_id, event),

    CONSTRAINT slack_notification_preferences_installation_fk
        FOREIGN KEY (workspace_id)
        REFERENCES slack_installations (workspace_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    -- The vocabulary. Named after what happened rather than after the screen
    -- that shows it, so that a rename in the UI is not a data migration.
    --
    -- `issue_priority_urgent` is a transition and not a state: the message is
    -- worth sending when something BECOMES urgent, and a channel that
    -- re-announced every urgent issue on every edit would be the noise this
    -- list is short to avoid.
    --
    -- `pull_request_merged` is spelled without a provider prefix on purpose.
    -- It is the only provider event here today and GitHub is the only provider
    -- that reports it, but the fact an admin is subscribing to is "the work
    -- landed" -- and a GitLab or Bitbucket integration would report the same
    -- fact, to the same channel, for the same reason. A `github_` prefix would
    -- make that a second row and a second toggle for one product concept.
    CONSTRAINT slack_notification_preferences_event_known
        CHECK (
            event IN (
                'issue_assigned',
                'issue_completed',
                'issue_priority_urgent',
                'project_health_changed',
                'project_update_published',
                'pull_request_merged'
            )
        )
);

-- No index here either. The primary key leads with `workspace_id`, which
-- serves both reads this table has -- "every preference for this workspace",
-- and the RESTRICT check when an installation is deleted -- and there is no
-- query that asks about one event across workspaces.
