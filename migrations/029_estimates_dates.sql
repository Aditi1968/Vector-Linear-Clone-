-- Estimates that mean something, and dates that do something.
--
-- Four changes, and one theme: migration 006 gave `issues` an `estimate` and a
-- `due_date` and then deliberately stopped. It fixed no unit for the estimate
-- ("a limit in the schema is a claim that some whole number of an unknown unit
-- is impossible, which is not a claim this file can make") and it gave the due
-- date no consequence at all -- a DATE that nothing reads, nothing warns about
-- and nothing regenerates. Both of those were the right call for 006 and both
-- are what this file finishes.
--
--   1. `teams.estimate_scale` -- the unit 006 refused to guess, asked of the
--      one place that can answer it.
--   2. `notifications_kind_known` widened by one, for the reminder.
--   3. `issue_due_reminders` -- the record that says an issue's due date has
--      already been warned about, which is what makes the warning happen once.
--   4. `issue_recurrences` -- a schedule hung off an `issue_templates` row, so
--      that a shape somebody saved can file itself.
--
-- Apply this ONLY through `python -m scripts.apply_migration
-- migrations/029_estimates_dates.sql`, for the reasons 002 sets out at length:
-- a hand-run gets no ledger row, no advisory lock and no recorded checksum, and
-- it executes statement-at-a-time in autocommit. Here that would mean the
-- notifications CHECK dropped and never replaced -- the inbox's vocabulary left
-- open for as long as it took somebody to notice -- and tables accepting rows
-- before the CHECKs that bound them exist.
--
-- DEPENDS ON:
--
--   * 002, for `teams` and `teams_workspace_id_key`.
--   * 006, for `issues.due_date` and `issues.archived_at`.
--   * 007, for `issues_workspace_id_key` -- the UNIQUE (workspace_id, id) the
--     reminder table's composite reference needs.
--   * 012, for `notifications` and the CHECK this file widens. Widening a
--     constraint that does not exist yet is a hard failure, which is the
--     dependency check doing its job.
--   * 020, for `issue_templates` and `issue_templates_workspace_id_key`.
--
-- Applying this before any of them fails on the constraint or column that needs
-- it and -- inside the single transaction the runner wraps the file in --
-- leaves nothing behind. That failure IS the dependency check: the ledger
-- records what has been applied and not what depends on what, so the schema
-- itself is what has to refuse an out-of-order apply.
--
--
-- WHAT IS DELIBERATELY NOT HERE: AN INDEX FOR DUE-DATE FILTERING
--
-- The product half of this work is `issues(filter: {due: OVERDUE})` and its
-- siblings, and they need no index because 015 already built the right one:
--
--     issues_workspace_live_due_date_id_idx
--         ON issues (workspace_id, due_date, id) WHERE archived_at IS NULL
--
-- Every predicate the filter emits is an equality or a RANGE on `due_date`
-- under a fixed `workspace_id` and `archived_at IS NULL` -- `due_date <
-- CURRENT_DATE`, `= CURRENT_DATE`, `>= CURRENT_DATE AND < CURRENT_DATE + 7`,
-- `BETWEEN $a AND $b`, `IS NULL` -- which is exactly the shape that index is a
-- bound scan for. `IS NULL` included: a btree indexes NULLs, so the undated
-- issues are a range at the far end of it rather than a predicate applied after
-- a scan, the same property 015 relies on for `assigneeId: null`.
--
-- The predicates are ANDed onto the tenant equality and never folded into the
-- keyset comparison, so nothing here changes what the ordering indexes serve.
-- A due filter under `orderBy: DUE_DATE` walks this index and stops; under the
-- default `createdAt DESC` the planner chooses between filtering this one and
-- walking `issues_workspace_live_created_at_id_idx` -- which is the ordinary
-- filter-versus-ordering trade 015 already describes and declines to buy
-- thirty-two indexes to remove.
--
--
-- WHOSE TODAY, AND WHY IT IS UTC
--
-- Every relative window above compares a DATE against `CURRENT_DATE`, and this
-- file is the place to say what that means, because the reminder sweep below
-- asks the same question and the two must not answer it differently.
--
-- A due date is a calendar day with no time and no zone -- 006 argues that at
-- length, and refuses TIMESTAMPTZ precisely so that "due Friday" is the same
-- promise to a colleague in Berlin and one in Los Angeles. The moment that
-- promise is compared against a viewer's LOCAL today, it stops being one
-- promise: the same issue is overdue for one of them and not the other, and two
-- people in a standup are reading different lists. So there is one clock, it is
-- the database server's, and every deployment of this application runs it in
-- UTC.
--
-- The cost is real and worth naming: a team at UTC+13 sees an issue become
-- overdue up to thirteen hours after their own midnight. That is a smaller
-- wrong than the list disagreeing between two colleagues, and it is the same
-- answer the reminder gives, so nothing in the product contradicts anything
-- else in it.
--
-- ponytail: no per-workspace timezone. There is no such column anywhere in this
-- schema and adding one is its own feature -- a column, a settings screen, and a
-- decision about what a member in a different zone from their workspace sees.
-- The upgrade path is `workspaces.timezone TEXT`, with `CURRENT_DATE` becoming
-- `(now() AT TIME ZONE w.timezone)::date` in the four predicates and the one
-- sweep that read it. Buy it the day somebody in Auckland complains, not before.


-- --------------------------------------------------------------------------
-- 1. The unit an estimate is in
-- --------------------------------------------------------------------------

-- What a team's estimates COUNT.
--
-- 006 stored `estimate INTEGER` with no unit, which means 21 is points to one
-- team, hours to another, and unlabelable to any screen that has to render it.
-- The unit is a team practice -- one workspace legitimately holds an
-- engineering team estimating in points and a support team estimating in hours
-- -- so it belongs on `teams` and not on `workspaces`. It is also resolvable
-- from any issue without ambiguity, because `issues.team_id` is NOT NULL: the
-- scale an estimate is in is always the scale of the team that owns the issue.
--
-- 'none' is the DEFAULT, and that word is doing the whole backfill. Every
-- estimate written before this migration was written under no scale at all, and
-- 'none' is defined below as "a whole number of whatever this team has agreed
-- offline" -- which is exactly what those rows already meant. So no existing
-- value changes meaning, no value becomes invalid, and there is no backfill
-- statement because there is nothing to backfill.
--
-- NOT NULL with a default rather than nullable, and this is the one place this
-- schema takes a default where 002 argues against one. 002's rule is about
-- TENANCY columns, where a default turns a forgotten workspace into a silent
-- write against the bootstrap tenant. This is a preference with a correct
-- neutral value that every existing row is already in, and making it nullable
-- would mean NULL and 'none' were two spellings of one state -- which is the
-- thing 020 refuses for `scope`/`team_id` and 006 refuses for
-- `archived_at`/a boolean.
--
-- TEXT with a CHECK rather than an ENUM, matching `workflow_states.type` in
-- 005, `notifications.kind` in 012 and `domain_events.kind` in 027: adding a
-- value to an ENUM cannot be used in the transaction that added it, while
-- widening a CHECK is an ordinary constraint swap.
ALTER TABLE teams ADD COLUMN estimate_scale TEXT NOT NULL DEFAULT 'none';

-- The four scales, and what each one CONSTRAINS rather than merely renders.
--
--   none    -- no unit. Any whole number the team likes, which is the state
--              every row predating this file is in and the state a team that
--              never opens the setting stays in.
--   points  -- relative effort. Any whole number, and deliberately no ceiling
--              and no Fibonacci set: 006 refuses to guess a ceiling for an
--              unknown unit, and pinning the ladder here would make a team that
--              already estimates 4 and 6 unable to edit its own issues.
--   hours   -- clock time. Any whole number, same reasoning.
--   tshirt  -- XS, S, M, L, XL, stored as 1..5. This is the scale that makes
--              the feature more than a label: the stored integer is an INDEX
--              into a fixed ladder, so 8 is not a small t-shirt, it is not a
--              t-shirt at all, and a write carrying it has to be refused.
--
-- WHERE THAT REFUSAL LIVES, and why it is not a CHECK on `issues`. The rule
-- relates two tables -- an issue's estimate and its team's scale -- and a CHECK
-- constraint cannot join. The two remaining options were a trigger, which this
-- project has deliberately not taken up (see `IssueService`'s note on the
-- completed_at rule, which is the same shape for the same reason), and the
-- write path. So `IssueService` validates an estimate against the scale
-- `app.domain.estimates` maps, on create and on update, and this column is what
-- it reads.
--
-- The consequence, stated rather than discovered: a team that SWITCHES to
-- tshirt keeps whatever estimates its issues already hold. The scale bounds
-- WRITES, not history. That is the deliberate choice -- the alternative is
-- either scanning every live issue to refuse the switch, which blocks a
-- legitimate product action for data nobody is editing, or rewriting estimates
-- a team spent real time agreeing. An out-of-scale estimate renders as its bare
-- number and conforms the next time somebody edits it.
ALTER TABLE teams ADD CONSTRAINT teams_estimate_scale_known
    CHECK (estimate_scale IN ('none', 'points', 'hours', 'tshirt'));


-- --------------------------------------------------------------------------
-- 2. The inbox vocabulary, widened by one
-- --------------------------------------------------------------------------

-- 012 admitted three kinds and argued for the shortness; 020 earned a fourth
-- with subscribers. This is the fifth, and it is the first that is not caused
-- by a person doing something.
--
-- 'due_soon' is the whole point of storing a due date. Until now the date was
-- inert: visible on an issue somebody already had open, and silent to everybody
-- who did not. A commitment nobody is reminded of is a commitment the product
-- recorded and then declined to help with.
--
-- Dropped and re-added rather than altered, which is the only way to widen a
-- CHECK -- and the reason 012 made it a CHECK rather than an enum type in the
-- first place. Both statements are inside the runner's single transaction, so
-- there is no instant at which the column is unconstrained.
--
-- Widening only. Every kind 012 and 020 admitted is still admitted, so no
-- existing row can fail the new constraint and the re-add's validation scan
-- cannot abort the migration.
ALTER TABLE notifications DROP CONSTRAINT notifications_kind_known;

ALTER TABLE notifications ADD CONSTRAINT notifications_kind_known
    CHECK (
        kind IN ('assigned', 'commented', 'blocked', 'status_changed', 'due_soon')
    );


-- --------------------------------------------------------------------------
-- 3. The reminder ledger
-- --------------------------------------------------------------------------

-- One row per (issue, the due date it was warned about).
--
-- THIS TABLE IS NOT A SCHEDULE. Nothing here says when a reminder will fire,
-- and that absence is the entire design -- it is what answers the question
-- "what happens when the due date moves after a reminder is scheduled", which
-- every scheduled-job version of this feature has to answer with a cancel path.
-- The answer here is: nothing is scheduled, so there is nothing to cancel. The
-- sweep reads each issue's CURRENT `due_date` on every pass and writes a row
-- naming it. Three consequences fall out and all three are the behaviour
-- somebody would otherwise have to write code for:
--
--   * a due date moved FORWARD is a new (issue, due_date) pair, so the issue is
--     warned about the new commitment. That is right -- it is a new promise.
--   * a due date moved BACK onto a day already warned about collides with the
--     row already here and stays silent. Also right: that person was told.
--   * a due date CLEARED simply stops matching the sweep's predicate. There is
--     no pending job holding a stale date.
--
-- EXACTLY ONCE UNDER TWO REPLICAS is the primary key plus `ON CONFLICT DO
-- NOTHING ... RETURNING`, which is `domain_events`' mechanism from 027 applied
-- to the same problem rather than a third one invented beside it. The sweep
-- INSERTs from a SELECT over the due issues and takes as its work exactly the
-- rows the INSERT returned; a second replica running the same statement in the
-- same instant blocks on the first's uncommitted key, then sees the conflict
-- and is returned nothing for that issue. The notification fan-out happens in
-- the SAME transaction as the insert, so the ledger row and the inbox items
-- commit together -- there is no window in which one exists without the other.
--
-- There is deliberately no lease, no attempt counter and no `next_attempt_at`,
-- which is what 028 needed and this does not. The work behind an
-- `embedding_jobs` claim is a model run of hundreds of milliseconds with no
-- connection held; the work behind a claim here is one INSERT ... SELECT on the
-- same connection. There is nothing to lease, because there is no gap.
CREATE TABLE issue_due_reminders (
    -- The tenant, and the only one this row can be about. Read by the composite
    -- reference below, so an issue from another workspace is not a row
    -- PostgreSQL will store -- the pattern 002 establishes and every table
    -- since restates.
    workspace_id UUID NOT NULL,

    issue_id UUID NOT NULL,

    -- The due date this reminder was ABOUT, which is what makes the key mean
    -- "already warned" rather than "warned at some point about something".
    --
    -- A DATE and not a timestamp, matching `issues.due_date` exactly, because
    -- it is a copy of that value at the moment the warning went out and a
    -- comparison between the two has to be an equality that can hold.
    due_date DATE NOT NULL,

    -- When the inbox items were written. Not part of the key -- the key is the
    -- promise, not the moment -- and here for an operator asking why somebody
    -- did or did not hear about something.
    reminded_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- The row IS its key, exactly as `issue_subscribers` in 020 and
    -- `embedding_jobs` in 028. A surrogate id would need a UNIQUE over these
    -- three columns beside it to say the same thing, which is a second key that
    -- buys nothing -- and the key is also the index the sweep's ON CONFLICT
    -- resolves against.
    CONSTRAINT issue_due_reminders_pkey
        PRIMARY KEY (workspace_id, issue_id, due_date),

    -- Composite through `workspace_id`, onto `issues_workspace_id_key` from
    -- 007. `REFERENCES issues (id)` is the obvious spelling and it is the bug:
    -- it would check that the id names an issue somewhere and say nothing about
    -- whose, which is how a reminder about another tenant's issue becomes
    -- storable and then fans out into inboxes.
    --
    -- ON DELETE RESTRICT, matching `notifications` and `issue_activity` in 012
    -- rather than 028's CASCADE, and the difference is what the row IS. An
    -- `embedding_jobs` row is a note saying somebody should do some work, and
    -- an issue that no longer exists is one nobody should work on. This is a
    -- record that a notification WAS SENT -- people received it, and it sits in
    -- their inboxes -- so it belongs with the other records of things that
    -- happened. Nothing deletes an issue in this product anyway; RESTRICT is
    -- the choice that makes whoever writes the first delete path decide out
    -- loud.
    CONSTRAINT issue_due_reminders_issue_fk
        FOREIGN KEY (workspace_id, issue_id)
        REFERENCES issues (workspace_id, id)
        ON DELETE RESTRICT ON UPDATE RESTRICT
);

-- There is deliberately no index beyond the primary key.
--
-- The sweep's only read of this table is an anti-join against it at
-- (workspace_id, issue_id, due_date), which is `issue_due_reminders_pkey`
-- itself; the same key serves the referencing side of
-- `issue_due_reminders_issue_fk`, since (workspace_id, issue_id) is its leading
-- prefix. Nothing reads `reminded_at`, and an index on it would be paid for on
-- every insert to answer a question nobody asks.
--
-- ponytail: no pruning. A reminder row lives forever, which is one row per
-- issue per due date it ever held -- bounded by how often people move dates, and
-- worth keeping as the answer to "was I told about this". The sweep only ever
-- probes it by key, so its size costs the sweep nothing. It belongs with
-- whatever eventually prunes `domain_events` and `github_deliveries`, which 027
-- and 017 say the same thing about.


-- --------------------------------------------------------------------------
-- 4. Recurring issues
-- --------------------------------------------------------------------------

-- A schedule, hung off the `issue_templates` row that says WHAT to file.
--
-- WHY THE TEMPLATE AND NOT THE ISSUE. The other design is an issue that clones
-- itself -- a `recurs_every` on `issues`, and the copy inherits it or does not.
-- It was rejected for three reasons, in order of how badly each one goes wrong:
--
--   * WHICH COPY IS THE MASTER has no good answer. If the clone inherits the
--     schedule there are now two sources and the count doubles weekly; if it
--     does not, the schedule lives on one particular issue -- which somebody
--     will complete, archive, or move to another team, and the recurrence goes
--     with it silently.
--   * A TEMPLATE IS ALREADY THE SHAPE OF AN ISSUE, and 020 built it to be
--     replayed: every id it carries -- assignee, project, cycle, labels -- is
--     held by a composite foreign key through `workspace_id` at SAVE time,
--     precisely because "written once and applied many times, possibly months
--     later" is what a template is for. That is the exact property a recurrence
--     needs and it already exists.
--   * AN ISSUE IS NOT A SHAPE. It has a status, a discussion, an assignee who
--     may since have left the workspace, and an identifier people wrote down in
--     commit messages. Editing next month's recurrence would mean editing a
--     particular issue from last month, and reading the schedule would mean
--     opening it.
--
-- A separate table rather than columns on `issue_templates`, for one reason
-- worth more than the extra join: a recurrence is all-or-nothing. Six nullable
-- columns on `issue_templates` would need a six-way CHECK to say "either every
-- one of these is set or none is", where here the row's EXISTENCE says it. It
-- also keeps the sweep's index proportional to the recurrences rather than to
-- every template in the installation, and a template with no schedule pays
-- nothing.
CREATE TABLE issue_recurrences (
    -- The tenant, read by both composite references below -- so the template
    -- and the team this recurrence files into are checked against the SAME
    -- workspace rather than against two that happen to be spelled separately.
    workspace_id UUID NOT NULL,

    -- One schedule per template, which is why the template IS the key. Two
    -- schedules for one shape is two things to reconcile when somebody edits
    -- the shape, and the second one is the one nobody remembers exists. A
    -- workspace that genuinely wants "the standup checklist, weekly AND on the
    -- first of the month" copies the template, which costs one row and is
    -- visible in the menu.
    template_id UUID NOT NULL,

    -- Where the generated issue lands.
    --
    -- NOT NULL even though `issue_templates.team_id` is nullable, and that is
    -- the point: a workspace-wide template has no team, and an issue must have
    -- one. `TemplateService.apply` already requires the caller to name a team
    -- for exactly this reason, and a background sweep has no caller to ask --
    -- so the schedule is where the answer is written down. For a TEAM-SCOPED
    -- template this must be that same team; the two values are both in hand
    -- when a recurrence is saved, so `TemplateService` refuses the mismatch
    -- there rather than leaving `apply` to refuse it obscurely at 3am.
    team_id UUID NOT NULL,

    -- 'daily', 'weekly' or 'monthly', from `issue_recurrences_frequency_known`.
    --
    -- Not a cron expression, and that is a deliberate ceiling rather than a
    -- first cut. A cron field is a small language with its own parser, its own
    -- error messages and its own surprises (the day-of-month/day-of-week OR
    -- rule catches everybody once), bought so that a project tracker can
    -- express "every third Tuesday in months divisible by four" -- which nobody
    -- has ever asked a project tracker for. These three plus the interval below
    -- cover what people actually schedule: a daily checklist, a weekly report,
    -- a fortnightly retro, a monthly invoice.
    frequency TEXT NOT NULL,

    -- The N in "every N days / weeks / months".
    --
    -- SMALLINT and bounded at 52, which is the honest width and an honest
    -- ceiling: 52 weeks is a year, 52 days is a quarter, and 52 months is
    -- longer than most of these templates will exist. The bound also bounds the
    -- forward scan `app.domain.recurrence.next_occurrence` performs, which is
    -- how that function stays a loop over a predicate instead of closed-form
    -- calendar arithmetic nobody can review.
    interval_count SMALLINT NOT NULL,

    -- WEEKLY ONLY: which days of the week, as ISO weekday numbers, 1 = Monday
    -- through 7 = Sunday.
    --
    -- An ARRAY, and 020 argues at length against arrays -- correctly, for IDS,
    -- because an array cannot carry a foreign key and every id in one would be
    -- unvalidated text. A weekday number references nothing. It is a bounded
    -- integer like `priority`, and a `issue_recurrence_weekdays` join table
    -- would be three columns and an index to store at most seven small numbers
    -- that are only ever read together with the row they belong to.
    --
    -- NULL for the other two frequencies, and the CHECK below makes that an
    -- equivalence rather than a convention -- so there is no such thing as a
    -- daily recurrence carrying weekdays nothing reads.
    weekdays SMALLINT[],

    -- MONTHLY ONLY: which day of the month, 1..31.
    --
    -- THE ANCHOR, and it has to be one. "The 31st" in a 30-day month is
    -- CLAMPED to the last day (see `app.domain.recurrence`) rather than
    -- skipped, because a monthly obligation that does not happen in February is
    -- never what anybody meant, and clamping loses no occurrence where skipping
    -- loses one a year.
    --
    -- What matters more is that this column, and never the previous instance,
    -- is what each next date is computed FROM. Computing "one month after the
    -- last run" walks the recurrence backwards -- Jan 31, Feb 28, Mar 28, and
    -- by June it is the 28th forever. Anchoring here means February clamps and
    -- March returns to the 31st.
    day_of_month SMALLINT,

    -- The day the schedule is anchored to, and the first day it may fire.
    --
    -- Load-bearing for any interval above 1, which is why it is a column and
    -- not a convenience. "Every two weeks on Tuesday" does not say WHICH
    -- Tuesdays without a week to count from, and "every three months on the
    -- 1st" does not say which months. The stride is counted from here: whole
    -- weeks between this row's week and a candidate's for weekly, whole months
    -- for monthly, whole days for daily.
    --
    -- It is also the "not before" bound, so a recurrence saved today for a
    -- quarter that starts in April files nothing in the meantime.
    starts_on DATE NOT NULL,

    -- The generated issue's due date, as a number of days after the day it is
    -- filed. NULL for a recurring issue with no due date, which is an ordinary
    -- state -- a daily checklist is due when it is due.
    --
    -- An OFFSET and not a date, because a schedule that stored a date would
    -- store one date and file many issues. Bounded at a year by the CHECK
    -- below: an offset longer than that is a due date further away than the
    -- next occurrence, which is a schedule that produces issues in an order
    -- nobody can read.
    due_in_days SMALLINT,

    -- THE ENTIRE SCHEDULER STATE, and the one column the sweep reads.
    --
    -- The claim is `SELECT ... WHERE next_run_on <= CURRENT_DATE FOR UPDATE
    -- SKIP LOCKED` followed by an UPDATE that moves this forward, both in one
    -- transaction -- `EventRepository.claim_next`'s shape exactly. Two replicas
    -- running it in the same instant take disjoint sets, and whichever commits
    -- first has moved this column past today, so the other's predicate no
    -- longer matches the row. That is what makes one occurrence produce one
    -- issue however many processes are draining.
    --
    -- A DATE and not a timestamp, which is also the whole DST answer: this
    -- feature has no clock. Every value it computes and stores is a calendar
    -- day, `app.domain.recurrence` is `datetime.date` arithmetic throughout,
    -- and a daylight-saving transition moves instants, not days. The one place
    -- a clock enters is `CURRENT_DATE` deciding which day today is, which is
    -- the same UTC answer the due-date filters and the reminder sweep give --
    -- see the note at the top of this file.
    next_run_on DATE NOT NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- The row IS its key, for the reason the column note gives: one schedule
    -- per template.
    CONSTRAINT issue_recurrences_pkey PRIMARY KEY (workspace_id, template_id),

    -- Composite through `workspace_id` onto `issue_templates_workspace_id_key`
    -- from 020, so a schedule cannot name another tenant's shape. That matters
    -- more here than in most tables, because the sweep is the one reader in
    -- this system with no tenant of its own: the workspace it files an issue
    -- into is the `workspace_id` it read OFF THIS ROW, never one a caller
    -- supplied, and this constraint is what makes that read trustworthy. The
    -- same argument 028 makes for `embedding_jobs_issue_fk`.
    --
    -- ON DELETE RESTRICT rather than CASCADE, matching
    -- `issue_template_labels_template_fk` beside it. `TemplateRepository.delete`
    -- removes this row itself, in the same transaction, before deleting the
    -- template -- so the constraint is a guard on that ordering rather than an
    -- obstacle to it, exactly as 009 describes for project_teams. CASCADE would
    -- mean a one-line template delete silently stops a schedule with nothing in
    -- the command tag saying so.
    CONSTRAINT issue_recurrences_template_fk
        FOREIGN KEY (workspace_id, template_id)
        REFERENCES issue_templates (workspace_id, id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    -- The team must be in this recurrence's own workspace. One `workspace_id`
    -- column feeds this and the template reference above, so a schedule pairing
    -- workspace A's template with workspace B's team is not a row that exists
    -- to be claimed.
    CONSTRAINT issue_recurrences_team_fk
        FOREIGN KEY (workspace_id, team_id)
        REFERENCES teams (workspace_id, id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    CONSTRAINT issue_recurrences_frequency_known
        CHECK (frequency IN ('daily', 'weekly', 'monthly')),

    CONSTRAINT issue_recurrences_interval_range
        CHECK (interval_count BETWEEN 1 AND 52),

    -- Weekdays exist for exactly one frequency, written as an EQUALITY between
    -- two tests rather than as two implications -- the shape 027 uses for
    -- `domain_events_delivered_has_an_instant`, and for the same reason: one
    -- expression to get right, closing both directions at once. A weekly
    -- recurrence with no weekdays is a schedule that never fires; a daily one
    -- carrying weekdays is a row two readers would disagree about.
    CONSTRAINT issue_recurrences_weekdays_match_frequency
        CHECK ((frequency = 'weekly') = (weekdays IS NOT NULL)),

    -- One to seven distinct ISO weekday numbers. `<@` is array containment, so
    -- this refuses 0 (which nothing here means by it -- ISO starts at 1) and 8
    -- in one test. The service sorts and deduplicates before writing, so the
    -- upper bound of seven is reachable only by a legitimate "every day of the
    -- week" and not by a client repeating Monday eight times.
    CONSTRAINT issue_recurrences_weekdays_valid
        CHECK (
            weekdays IS NULL
            OR (
                array_length(weekdays, 1) BETWEEN 1 AND 7
                AND weekdays <@ ARRAY[1, 2, 3, 4, 5, 6, 7]::SMALLINT[]
            )
        ),

    -- The same equivalence for the monthly anchor.
    CONSTRAINT issue_recurrences_day_of_month_matches_frequency
        CHECK ((frequency = 'monthly') = (day_of_month IS NOT NULL)),

    CONSTRAINT issue_recurrences_day_of_month_range
        CHECK (day_of_month IS NULL OR day_of_month BETWEEN 1 AND 31),

    -- Zero is allowed and means "due the day it is filed", which is what a
    -- daily task usually is. The ceiling is a year; see the column note.
    CONSTRAINT issue_recurrences_due_in_days_range
        CHECK (due_in_days IS NULL OR due_in_days BETWEEN 0 AND 365),

    -- A schedule cannot be due before it starts. The same shape as
    -- `workspace_members_removed_after_created` in 026: not a plausible row
    -- that happens to be unusual, but one whose two dates contradict each other
    -- -- and the write that produced it had a bug. `>=` because the first
    -- occurrence is frequently the start day itself.
    CONSTRAINT issue_recurrences_runs_after_start
        CHECK (next_run_on >= starts_on)
);

-- The claim index, and the only read this table has that is not a seek on its
-- primary key.
--
-- `next_run_on` leads and `workspace_id` does not, which is the one shape in
-- this schema where the tenant does not lead -- and it is the same exception
-- 028 makes for `embedding_jobs_due_idx`, for the same reason. A background
-- sweep has no tenant. It is not serving a request, no caller's membership
-- could scope it, and its whole job is to work through every workspace's due
-- schedules. An index led by `workspace_id` would make "the oldest due row
-- anywhere" a scan whose cost grows with the number of workspaces rather than
-- with the amount of work outstanding.
--
-- Tenancy is not weakened by that, because nothing about this ordering reaches
-- a caller: the rows are consumed by the sweep and turned into an apply whose
-- workspace came off the row itself. `template_id` trails to make the ordering
-- total, so two rows sharing a `next_run_on` -- which is most of them, since
-- these are dates -- have an agreed order for anybody debugging a claim.
CREATE INDEX issue_recurrences_due_idx
    ON issue_recurrences (next_run_on, workspace_id, template_id);

-- The referencing side of `issue_recurrences_team_fk`. PostgreSQL indexes the
-- REFERENCED side of a foreign key and never the referencing side, so without
-- this every `DELETE FROM teams` scans this table in full to satisfy the
-- RESTRICT. The template reference needs no index of its own:
-- (workspace_id, template_id) IS `issue_recurrences_pkey`.
CREATE INDEX issue_recurrences_workspace_team_idx
    ON issue_recurrences (workspace_id, team_id);

-- There is deliberately no reference to `workspace_members` anywhere in this
-- file, which is worth saying because 026 makes every new one the application's
-- problem: a table referencing that one gets no removal veto from PostgreSQL
-- any more, and needs a probe added to `MembershipRepository.shared_holdings`
-- and an entry in `_REMOVAL_BLOCKED`. Nothing here names a person. A reminder
-- is addressed through `notifications`, which 012 already holds; a recurrence
-- files an issue whose assignee comes from the template, which
-- `issue_templates_assignee_fk` already covers and 026 already probes. So a
-- member removal is unaffected by this migration, and that is by construction
-- rather than by luck.
