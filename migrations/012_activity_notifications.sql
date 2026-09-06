-- What happened to an issue, and what one person still has to look at.
--
-- Two tables, and they are deliberately not one. `issue_activity` is an
-- append-only history of an ENTITY -- "the state went from A to B, by U, at
-- T" -- and it is the same row for everybody who reads the issue.
-- `notifications` is a per-PERSON inbox with a read state, and the same event
-- produces one row per recipient or none at all. Collapsing them gives a
-- history that grows a `read_at` column meaning nothing to fifteen of the
-- sixteen people who can see it, or an inbox that has to be filtered back
-- down to a history by a query nobody can index.
--
-- Neither is `comments`, which migration 007 created and this file does not
-- touch. 007's own comment on that table says it: a comment is something a
-- person WROTE and may delete; this is a record of what the system DID.
--
-- Apply this ONLY through `python -m scripts.apply_migration
-- migrations/012_activity_notifications.sql`. The runner wraps the file in
-- one transaction, takes an advisory lock and records a checksum; a hand-run
-- gets none of that and executes statement-at-a-time in autocommit, which
-- here means a failure part way through leaves one of the two tables standing
-- with no ledger row to say so.
--
-- DEPENDS ON:
--
--   * 002, for `workspaces` and the tenancy columns on `issues`.
--   * 003, for `users`. Both `actor_id` columns reference it directly; see
--     the note on issue_activity_actor_fk for why they do not reference
--     `workspace_members` the way every other person-column here does.
--   * 004, for `workspace_members`. `notifications (workspace_id, user_id)`
--     references its primary key.
--   * 007, for `issues_workspace_id_key` -- the UNIQUE (workspace_id, id) on
--     `issues` that both composite issue references below target. 007
--     declares it and this file must not: a duplicate constraint name is a
--     hard failure for whichever migration runs second.
--
-- Applying this before any of them fails on the constraint that needs it and,
-- inside the runner's single transaction, leaves nothing behind. That failure
-- IS the dependency check: the ledger records what has been applied, not what
-- depends on what.


-- The history. One row per thing that happened to one issue.
--
-- Explicit typed columns rather than a JSONB payload, and the choice is
-- forced by the shape of the events rather than by a preference. Every kind
-- below is one scalar moving from one value to another, so `from_value` and
-- `to_value` say all of it -- and they say it in a form a CHECK can constrain,
-- an index can order and a reader can grep. JSONB would buy the freedom to
-- store a different shape per kind, which is the freedom for two writers to
-- disagree about the shape of the same kind with nothing to notice.
--
-- TEXT and not a per-kind type, because the two columns hold a title, a
-- priority and a uuid depending on the row. That is the one thing this design
-- gives up, and it is bounded: the columns are opaque to the database, never
-- joined on and never compared -- `issue_activity` answers exactly one query,
-- and it is "this issue's history, newest first".
CREATE TABLE issue_activity (
    id UUID PRIMARY KEY DEFAULT uuidv7(),

    -- Denormalised from the issue so that issue_activity_issue_fk can be
    -- composite, and so a read is scoped by tenant without a join. The value
    -- is not independent -- that foreign key forces it to equal the issue's
    -- own workspace -- so it cannot drift.
    workspace_id UUID NOT NULL,
    issue_id UUID NOT NULL,

    -- Who did it, or nobody. NULL is not a missing value to be backfilled: it
    -- is a system action -- an import, a scheduled transition, a future
    -- automation -- and a history that could not record one would have to
    -- either invent an actor or drop the event.
    actor_id UUID,

    kind TEXT NOT NULL,

    -- The scalar before and after. Both nullable, and NULL means different
    -- things per kind, which is why the kinds are enumerated below rather
    -- than left open: 'created' and 'archived' carry neither, an
    -- 'assignee_changed' that unassigns carries a NULL `to_value` that is the
    -- event, and 'commented' carries only the comment's id in `to_value`.
    from_value TEXT,
    to_value TEXT,

    -- No updated_at. This table is append-only by construction -- nothing in
    -- the application issues an UPDATE against it -- and a column recording
    -- when a historical fact was last rewritten would be an invitation to
    -- rewrite one.
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- The composite key that makes a cross-tenant history row
    -- unrepresentable: a row claiming workspace A about an issue owned by B
    -- has no workspace_id that satisfies this reference.
    --
    -- ON DELETE RESTRICT, unlike 007's CASCADE on `comments`. The two differ
    -- because of what is lost. A comment on a deleted issue is unreachable,
    -- so CASCADE discards nothing anyone could have read; a history is the
    -- record OF the deletion's subject, and taking it with the row is how an
    -- audit trail turns out to be missing exactly the entity somebody asked
    -- about. Nothing deletes an issue today -- this product archives, for the
    -- reasons 006 gives -- so the refusal costs nothing and is there for
    -- whoever writes the first delete path to have to think about.
    CONSTRAINT issue_activity_issue_fk
        FOREIGN KEY (workspace_id, issue_id)
        REFERENCES issues (workspace_id, id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    -- The actor references `users` directly, NOT `workspace_members`, and
    -- this is the one place in this file that departs from the composite
    -- pattern. It is 006's argument about `issues.creator_id`, applied to a
    -- table that is entirely made of that kind of fact: "who did this" is a
    -- historical claim about a moment that has already happened, and it stays
    -- true after the person leaves the workspace. A composite key onto
    -- `workspace_members` would either forbid offboarding anyone who ever
    -- touched an issue, or -- under SET NULL -- erase them from the history
    -- of everything they did on the way out.
    --
    -- What that gives up is the database-level guarantee that an actor was a
    -- member of the workspace they acted in. That fact is enforced where it
    -- is live: an actor arrives from an authenticated session that the
    -- resolver has already resolved a workspace for, and every write path
    -- here sits behind one. Recording it does not re-assert it forever.
    --
    -- ON DELETE SET NULL, bare because there is one referencing column.
    -- Deleting an account leaves its actions in the history attributed to
    -- nobody, which is the state this column already defines for a system
    -- action and which the read path therefore already handles.
    CONSTRAINT issue_activity_actor_fk
        FOREIGN KEY (actor_id)
        REFERENCES users (id)
        ON DELETE SET NULL ON UPDATE RESTRICT,

    -- The vocabulary, closed. A CHECK rather than an enum type for the reason
    -- 009 gives at projects_state_check: the runner wraps a file in one
    -- transaction and PostgreSQL refuses to use an enum label added by
    -- `ALTER TYPE ... ADD VALUE` in the transaction that added it, so a
    -- thirteenth kind would need a migration split across two files.
    --
    -- Closed rather than open because an open `kind` is how a typo becomes a
    -- history nobody can query: 'state_change' and 'state_changed' both store
    -- fine, and the timeline silently loses half its rows.
    CONSTRAINT issue_activity_kind_known CHECK (
        kind IN (
            'created',
            'title_changed',
            'state_changed',
            'priority_changed',
            'assignee_changed',
            'archived',
            'commented',
            'label_attached',
            'label_detached',
            'relation_added',
            'project_changed',
            'cycle_changed'
        )
    )
);

-- The one read this table serves: one issue's history, newest first.
--
-- All four columns are load-bearing. (workspace_id, issue_id) is the
-- equality, and (created_at DESC, id DESC) is the keyset the cursor compares
-- against, with `id` breaking a created_at tie so the ordering is total and a
-- page walk can neither repeat nor skip a row. Descending, unlike 007's
-- comment index: a discussion reads forwards, a history reads backwards.
--
-- Its (workspace_id, issue_id) prefix is also what the referential check
-- behind issue_activity_issue_fk looks up, so this one index answers the
-- product query and keeps an issue deletion from scanning the whole table.
CREATE INDEX issue_activity_workspace_issue_created_idx
    ON issue_activity (workspace_id, issue_id, created_at DESC, id DESC);

-- The referencing side of issue_activity_actor_fk. PostgreSQL indexes the
-- REFERENCED side of a foreign key and never the referencing side, so without
-- this every account deletion scans issue_activity in full to find the rows
-- its SET NULL must rewrite. Not composite with workspace_id, because the
-- foreign key is not: the lookup the referential action performs is on
-- `actor_id` alone.
CREATE INDEX issue_activity_actor_idx ON issue_activity (actor_id);


-- One person's inbox, in one workspace.
--
-- A notification is not an event; it is a claim that a particular user has
-- something to look at. So it is written only for people who did not cause it
-- (see notifications_actor_is_not_recipient) and only for the kinds a person
-- would actually want interrupting them, which is why this vocabulary is
-- three names and the history's is twelve.
CREATE TABLE notifications (
    id UUID PRIMARY KEY DEFAULT uuidv7(),

    workspace_id UUID NOT NULL,

    -- The recipient. Composite against `workspace_members` below, not
    -- `users`: unlike an actor, this is a live claim about the present -- an
    -- inbox in THIS workspace -- and it stops being true the moment the
    -- person leaves. That is 006's distinction between `assignee_id` and
    -- `creator_id`, and it lands on the same side.
    user_id UUID NOT NULL,

    -- Who caused it, or nobody for a system action. References `users` for
    -- the same reason the history's actor does, and with the same tradeoff.
    actor_id UUID,

    -- NOT NULL: every kind below is about an issue. The draft schema left
    -- this nullable for workspace invites and digests, which would need
    -- MATCH SIMPLE reasoning on the composite key and do not exist -- a
    -- nullable column with no NULL rows is a case every reader has to
    -- consider and no writer ever produces.
    issue_id UUID NOT NULL,

    kind TEXT NOT NULL,

    -- NULL is unread, and the timestamp IS the read state. There is
    -- deliberately no `is_read BOOLEAN` beside it: two columns encoding one
    -- fact is two columns that can disagree, and the disagreement surfaces as
    -- a badge count that does not match the list under it. Marking read is
    -- `read_at = COALESCE(read_at, now())`, so a second mark keeps the first
    -- instant rather than moving it.
    read_at TIMESTAMPTZ,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- A notification cannot point at another tenant's issue: one workspace_id
    -- column feeds this reference and the membership one below, so both are
    -- checked against the same workspace.
    --
    -- ON DELETE RESTRICT, matching the history above rather than 007's
    -- CASCADE, and for a weaker reason: nothing deletes an issue, so the
    -- choice is between two refusals no path reaches. RESTRICT is the one
    -- that makes whoever writes the first delete path decide out loud.
    CONSTRAINT notifications_issue_fk
        FOREIGN KEY (workspace_id, issue_id)
        REFERENCES issues (workspace_id, id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    -- The recipient must be a member of this notification's workspace.
    --
    -- `REFERENCES users (id)` is the obvious spelling and it is the leak: it
    -- checks that the recipient is a real account somewhere and says nothing
    -- about where, so a notification about workspace A's issue could be filed
    -- into the inbox of somebody with no access to A -- and the inbox query
    -- would then hand them the issue id, the actor and the timing of work
    -- they cannot see.
    --
    -- ON DELETE RESTRICT, matching 007's comments_author_fk and with the same
    -- consequence stated out loud: a member with notifications cannot be
    -- removed from the workspace until they are cleared. Whoever writes the
    -- "remove a member" path owns that choice -- for an inbox, unlike a
    -- discussion, deleting the rows is almost certainly the right answer --
    -- and this file will not make it silently.
    CONSTRAINT notifications_user_fk
        FOREIGN KEY (workspace_id, user_id)
        REFERENCES workspace_members (workspace_id, user_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    CONSTRAINT notifications_actor_fk
        FOREIGN KEY (actor_id)
        REFERENCES users (id)
        ON DELETE SET NULL ON UPDATE RESTRICT,

    CONSTRAINT notifications_kind_known
        CHECK (kind IN ('assigned', 'commented', 'blocked')),

    -- Nobody is notified of their own action. The rule lives in the service
    -- that decides recipients, and it lives here too because it is the kind
    -- of rule that is right in every service until one forgets -- and the
    -- symptom is not an error, it is an inbox that reads back everything its
    -- owner just did.
    --
    -- The NULL branch is the system action, which has no actor to compare.
    CONSTRAINT notifications_actor_is_not_recipient
        CHECK (actor_id IS NULL OR actor_id <> user_id)
);

-- The badge and the unread inbox, both.
--
-- Partial on `read_at IS NULL`, which is what makes the count cheap and keeps
-- it cheap: the read backlog grows without bound and is never counted, so
-- excluding it from the index means the count reads an index whose size
-- tracks the unread rows rather than the table. The predicate is written the
-- way the query writes it, so the planner matches it without an implication
-- proof -- which is the property 009 declined to rely on for a referential
-- check it does not author, and can rely on here because this predicate is
-- ours.
--
-- (created_at DESC, id DESC) is the keyset, ordered so the page is read
-- rather than sorted, with `id` making the ordering total.
CREATE INDEX notifications_workspace_user_unread_idx
    ON notifications (workspace_id, user_id, created_at DESC, id DESC)
    WHERE read_at IS NULL;

-- The whole inbox, read and unread, which is the same query with
-- `unreadOnly: false`. A second index over almost the same thing, and not a
-- duplicate of the one above: a partial index cannot serve a query that does
-- not carry its predicate, so without this the unfiltered listing falls back
-- to a scan of every notification in the workspace.
--
-- Its (workspace_id, user_id) prefix is also the referencing side of
-- notifications_user_fk, so a member removal evaluates its RESTRICT against
-- this index rather than against the table.
CREATE INDEX notifications_workspace_user_created_idx
    ON notifications (workspace_id, user_id, created_at DESC, id DESC);

-- The referencing side of notifications_issue_fk and of
-- notifications_actor_fk. Neither is a query the product issues; both are
-- lookups PostgreSQL performs on its own behalf when a referenced row is
-- deleted, and without an index each one scans this table in full.
CREATE INDEX notifications_workspace_issue_idx
    ON notifications (workspace_id, issue_id);

CREATE INDEX notifications_actor_idx ON notifications (actor_id);
