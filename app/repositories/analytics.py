"""Aggregates over one workspace's issues.

Six statements, each one a plain `GROUP BY` over `issues` narrowed to a single
tenant. Nothing here is denormalised, cached, or precomputed; see
migrations/031_analytics.sql for the argument against a roll-up table and for
the one index that makes querying the truth affordable instead.

The repository receives a connection from the service layer. It never acquires
connections, never touches the pool, and never owns a transaction.
`asyncpg.Record` never escapes this class.

WHY EVERY STATEMENT JOINS `workflow_states`
-------------------------------------------
`issues.completed_at` means "when work STOPPED", not "when work succeeded":
`IssueService`'s completed_at rule stamps it for both terminal categories, so
a cancellation carries one exactly as a delivery does. Every query below that
means *finished* therefore says `workflow_states.type = 'completed'` and never
`completed_at IS NOT NULL` alone. Reading the timestamp on its own would have
reported every abandoned issue as throughput, which is the single most
plausible wrong number this feature could have produced.

The join is on all three columns of `issues_workflow_state_fk` -- workspace,
team and state -- because that is the key migration 005 declares and the index
`issues_workflow_state_idx` covers. Joining on `id` alone would work today and
would be a lookup that does not carry the tenant.

THE TENANT PREDICATE
--------------------
Every statement below begins `WHERE issues.workspace_id = $1`, and $1 is
always `scope.workspace_id` -- never a value that arrived with the request.
An aggregate is where a missing tenant predicate hides best: a cross-tenant
`count(*)` returns one plausible integer rather than a visibly foreign row, so
there is nothing on the screen for anyone to notice. The predicate is on the
scanned table in every one of the six, and the `workflow_states`, `teams` and
`users` joins are all narrowed by it in turn.
"""

from datetime import date, datetime

import asyncpg

from app.domain.analytics import (
    AssigneeLoad,
    CategoryCount,
    CycleProgress,
    CycleTimeSummary,
    DurationSummary,
    PriorityCount,
    ProjectProgress,
    TeamCompletion,
    ThroughputDay,
    dense_throughput,
)
from app.domain.estimates import EstimateScale
from app.domain.issues import TERMINAL_STATE_CATEGORIES
from app.domain.teams import WorkflowStateCategory
from app.domain.tenancy import WorkspaceScope


# The join every statement here makes, written once.
#
# A module constant interpolated into six f-strings rather than six copies,
# because it is the tenant-carrying half of each of them and a copy that lost
# a column would still run.
_STATE_JOIN = """
JOIN workflow_states
    ON workflow_states.workspace_id = issues.workspace_id
    AND workflow_states.team_id = issues.team_id
    AND workflow_states.id = issues.workflow_state_id
"""

# Seconds to hours, as a float. The database returns an interval's epoch in
# seconds and the product talks in hours; dividing here rather than on the
# client keeps one definition of the unit.
_SECONDS_PER_HOUR = 3600.0


class AnalyticsRepository:
    """Read-only SQL for the analytics screen. Nothing here writes."""

    async def creation_series(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        start: datetime,
        end: datetime,
    ) -> dict[date, int]:
        """Issues FILED per UTC day, as a sparse map the caller densifies.

        A separate statement from `completion_series` and not a column added to
        it, because the two bucket on different columns -- `created_at` here,
        `completed_at` there -- and one GROUP BY cannot place a row on two
        calendars. An issue filed in January and finished in March belongs to a
        January bar and a March bar, and a single statement would have to pick
        one.

        No `archived_at` predicate and no state join. This is a count of
        filing, which is an event that happened; nothing that happens to the
        issue afterwards un-files it.

        Served by `issues_workspace_created_at_id_idx` (002), which is why 031
        adds no second index for this half. The completion half needed one
        because nothing in 001-029 leads on `completed_at`; this half was
        already covered.
        """
        rows = await connection.fetch(
            """
            SELECT
                (issues.created_at AT TIME ZONE 'UTC')::date AS day,
                count(*) AS created
            FROM issues
            WHERE issues.workspace_id = $1
                AND issues.created_at >= $2
                AND issues.created_at < $3
            GROUP BY day
            """,
            scope.workspace_id,
            start,
            end,
        )

        return {row["day"]: row["created"] for row in rows}

    async def completion_series(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        start: datetime,
        end: datetime,
        range_start: date,
        days: int,
        created: dict[date, int],
    ) -> list[ThroughputDay]:
        """One row per UTC day in the window, zero-filled.

        The bucket expression is `AT TIME ZONE 'UTC'` and not `date_trunc`
        against the session zone. A session's `TimeZone` is whatever the server
        or the connection string left it as, so bucketing by it would make the
        same workspace report different numbers from two deployments of the
        same code -- a difference nothing on the screen could explain. UTC is
        arbitrary but it is fixed, and `analytics_window` says so out loud.

        The range predicate is half-open, so a completion at exactly midnight
        falls in one bucket. It is served by
        `issues_workspace_completed_at_idx` (031); without that index this is
        the query that reads the workspace's whole history.

        No `archived_at` predicate. Archiving does not un-finish work -- see
        031 on why the index is not partial on it either.

        Zero-filling happens in `app.domain.analytics.dense_throughput` rather
        than here: a `generate_series` LEFT JOIN would put the calendar in the
        statement, and the calendar is the part worth testing without a
        container.
        """
        rows = await connection.fetch(
            f"""
            SELECT
                (issues.completed_at AT TIME ZONE 'UTC')::date AS day,
                count(*) FILTER (
                    WHERE workflow_states.type = 'completed'
                ) AS completed,
                count(*) FILTER (
                    WHERE workflow_states.type = 'canceled'
                ) AS canceled
            FROM issues
            {_STATE_JOIN}
            WHERE issues.workspace_id = $1
                AND issues.completed_at >= $2
                AND issues.completed_at < $3
            GROUP BY day
            """,
            scope.workspace_id,
            start,
            end,
        )

        return dense_throughput(
            {row["day"]: (row["completed"], row["canceled"]) for row in rows},
            created,
            range_start,
            days,
        )

    async def lead_time(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        start: datetime,
        end: datetime,
    ) -> DurationSummary | None:
        """Median and p90 hours from creation to completion, or nothing.

        LEAD time, and the name matters: both instants are columns on the row,
        so every delivered issue is in the sample and there is no coverage to
        report. `cycle_time` below is the one that starts when work started,
        and it is the one with a gap.

        Over DELIVERED work only -- `type = 'completed'` -- because an issue
        abandoned after six months did not take six months to deliver, and
        including cancellations would make a team that closes stale issues look
        slower than one that leaves them open forever.

        `percentile_cont` interpolates between the two neighbouring samples
        rather than picking one of them (`percentile_disc`). On an even-sized
        sample the discrete form has to choose a side, and which side it
        chooses is not something a reader of a chart can see.

        Returns None on an empty sample rather than a summary of zeros. A
        zeroed summary renders identically to a real one that happens to be
        fast, and telling those apart is the entire point of this feature.
        """
        row = await connection.fetchrow(
            f"""
            SELECT
                count(*) AS sampled,
                percentile_cont(0.5) WITHIN GROUP (
                    ORDER BY EXTRACT(
                        EPOCH FROM (issues.completed_at - issues.created_at)
                    )
                ) AS median_seconds,
                percentile_cont(0.9) WITHIN GROUP (
                    ORDER BY EXTRACT(
                        EPOCH FROM (issues.completed_at - issues.created_at)
                    )
                ) AS p90_seconds
            FROM issues
            {_STATE_JOIN}
            WHERE issues.workspace_id = $1
                AND issues.completed_at >= $2
                AND issues.completed_at < $3
                AND workflow_states.type = 'completed'
            """,
            scope.workspace_id,
            start,
            end,
        )

        # An aggregate with no GROUP BY always returns exactly one row, so the
        # None branch is unreachable -- but `fetchrow` is typed as optional and
        # indexing a None is an AttributeError inside a resolver, which the
        # schema masks as "Internal server error".
        if row is None or row["sampled"] == 0:
            return None

        return DurationSummary(
            count=row["sampled"],
            median_hours=row["median_seconds"] / _SECONDS_PER_HOUR,
            p90_hours=row["p90_seconds"] / _SECONDS_PER_HOUR,
        )

    async def cycle_time(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        start: datetime,
        end: datetime,
    ) -> CycleTimeSummary | None:
        """Time spent being worked on, and how much of the work it covers.

        There is no `started_at` on `issues` -- no migration adds one -- so the
        start instant comes from `issue_activity` (012), which records a
        `state_changed` row carrying the workflow-state ids as TEXT in
        `from_value` and `to_value`. The earliest transition INTO a state whose
        category is `started` is the moment work began.

        LEFT JOIN LATERAL, and that is the honesty. An inner join would drop
        every issue with no recorded start and leave a median over the
        remainder looking like a median over all of it. Kept as an outer join,
        `count(*)` counts the completions and `count(started.at)` counts the
        measurable ones, so both halves of "41 of 96" come from one scan of one
        population and cannot disagree.

        `percentile_cont` ignores NULL inputs, so unmeasured issues contribute
        to the denominator and not to the percentiles, which is exactly right.

        THE JOIN BACK TO `workflow_states` IS ON TEXT, deliberately:
        `entered.id::text = moves.to_value` rather than `moves.to_value::uuid`.
        `to_value` is opaque TEXT whose meaning depends on `kind` -- a
        `commented` row holds a comment id, a `title_changed` row holds a title
        -- and casting it to UUID is an expression the planner may evaluate
        before the `kind` filter has excluded those rows. That failure is a
        22P02 raised out of an aggregate, not a wrong number, but it would be
        raised by whichever workspace happened to have a title with a
        non-UUID-shaped value in it. Casting the id to text instead cannot
        fail. `workflow_states` holds a handful of rows per team, so the index
        this gives up was never going to be the cost of this query.

        The lateral is narrowed by `workspace_id` as well as `issue_id`, which
        is redundant against `issue_activity_issue_fk` and is what makes the
        composite index `issue_activity_workspace_issue_created_idx` (012)
        usable rather than merely correct.
        """
        row = await connection.fetchrow(
            f"""
            SELECT
                count(*) AS completed_total,
                count(started.at) AS sampled,
                percentile_cont(0.5) WITHIN GROUP (
                    ORDER BY EXTRACT(EPOCH FROM (issues.completed_at - started.at))
                ) AS median_seconds,
                percentile_cont(0.9) WITHIN GROUP (
                    ORDER BY EXTRACT(EPOCH FROM (issues.completed_at - started.at))
                ) AS p90_seconds
            FROM issues
            {_STATE_JOIN}
            LEFT JOIN LATERAL (
                SELECT min(moves.created_at) AS at
                FROM issue_activity AS moves
                JOIN workflow_states AS entered
                    ON entered.workspace_id = moves.workspace_id
                    AND entered.team_id = issues.team_id
                    AND entered.id::text = moves.to_value
                WHERE moves.workspace_id = issues.workspace_id
                    AND moves.issue_id = issues.id
                    AND moves.kind = 'state_changed'
                    AND entered.type = 'started'
            ) AS started ON TRUE
            WHERE issues.workspace_id = $1
                AND issues.completed_at >= $2
                AND issues.completed_at < $3
                AND workflow_states.type = 'completed'
            """,
            scope.workspace_id,
            start,
            end,
        )

        # Nothing delivered means nothing to report a coverage OF, so the whole
        # summary is absent rather than "0 of 0".
        if row is None or row["completed_total"] == 0:
            return None

        # Delivered work exists but none of it has a recorded start: the
        # percentiles are NULL and there is no median to publish. The reader is
        # told this through the field being absent, and `lead_time` beside it
        # is the number that still works.
        if row["sampled"] == 0:
            return CycleTimeSummary(
                measured=0,
                completed_total=row["completed_total"],
                median_hours=0.0,
                p90_hours=0.0,
            )

        return CycleTimeSummary(
            measured=row["sampled"],
            completed_total=row["completed_total"],
            median_hours=row["median_seconds"] / _SECONDS_PER_HOUR,
            p90_hours=row["p90_seconds"] / _SECONDS_PER_HOUR,
        )

    async def issue_age(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
    ) -> DurationSummary | None:
        """How long the unfinished work has been open, right now.

        A snapshot with no window, like `state_mix`: the question is how stale
        the backlog is today, and an issue filed two years ago is two years old
        whatever window the reader picked.

        `now()` rather than the service's `today`, because this is an age in
        hours and rounding the upper end to a calendar day would make every age
        wrong by up to a day in the direction of younger.

        Unfinished and unarchived, which is the same population as `workload`
        and `priority_mix`. Including completed issues would measure how long
        finished work took, which is `lead_time` and is already published.
        """
        row = await connection.fetchrow(
            f"""
            SELECT
                count(*) AS sampled,
                percentile_cont(0.5) WITHIN GROUP (
                    ORDER BY EXTRACT(EPOCH FROM (now() - issues.created_at))
                ) AS median_seconds,
                percentile_cont(0.9) WITHIN GROUP (
                    ORDER BY EXTRACT(EPOCH FROM (now() - issues.created_at))
                ) AS p90_seconds
            FROM issues
            {_STATE_JOIN}
            WHERE issues.workspace_id = $1
                AND issues.archived_at IS NULL
                AND workflow_states.type <> ALL($2::TEXT[])
            """,
            scope.workspace_id,
            list(TERMINAL_STATE_CATEGORIES),
        )

        if row is None or row["sampled"] == 0:
            return None

        return DurationSummary(
            count=row["sampled"],
            median_hours=row["median_seconds"] / _SECONDS_PER_HOUR,
            p90_hours=row["p90_seconds"] / _SECONDS_PER_HOUR,
        )

    async def priority_mix(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
    ) -> list[PriorityCount]:
        """Live, unfinished issues per priority level.

        Bounded without a LIMIT, and that is not an oversight:
        `issues_priority_range` (001) constrains the column to 0-4, so this
        GROUP BY can return at most five rows however large the workspace is.
        It is the one breakdown here whose cardinality the schema already caps.

        The same population as `workload` -- unfinished and unarchived --
        because the two are read side by side and a reader comparing "who is
        carrying what" against "how urgent is it" would otherwise be comparing
        two different piles.

        Levels with no issues are absent rather than zero, as in `state_mix`:
        the client renders a fixed five-level axis, so the axis is defined in
        one place.
        """
        rows = await connection.fetch(
            f"""
            SELECT issues.priority AS priority, count(*) AS issues
            FROM issues
            {_STATE_JOIN}
            WHERE issues.workspace_id = $1
                AND issues.archived_at IS NULL
                AND workflow_states.type <> ALL($2::TEXT[])
            GROUP BY issues.priority
            ORDER BY issues.priority
            """,
            scope.workspace_id,
            list(TERMINAL_STATE_CATEGORIES),
        )

        return [
            PriorityCount(priority=row["priority"], issues=row["issues"])
            for row in rows
        ]

    async def state_mix(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
    ) -> list[CategoryCount]:
        """Live issues per workflow-state category, right now.

        No window: this is a snapshot and is labelled as one on the screen. The
        historical version of it needs a complete state history that begins at
        migration 012 -- see the note at the foot of `app.domain.analytics`.

        Categories with no issues are absent from the result rather than
        returned as zeros, because the caller renders a fixed five-category
        axis and filling the gaps there means the axis is defined in one place.
        """
        rows = await connection.fetch(
            f"""
            SELECT workflow_states.type AS category, count(*) AS issues
            FROM issues
            {_STATE_JOIN}
            WHERE issues.workspace_id = $1
                AND issues.archived_at IS NULL
            GROUP BY workflow_states.type
            """,
            scope.workspace_id,
        )

        return [
            CategoryCount(
                category=WorkflowStateCategory(row["category"]),
                issues=row["issues"],
            )
            for row in rows
        ]

    async def workload(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        limit: int,
    ) -> tuple[list[AssigneeLoad], int]:
        """The busiest `limit` assignees of unfinished work, and how many there
        are in total.

        "Unfinished" is the complement of `TERMINAL_STATE_CATEGORIES`: backlog,
        unstarted and started. Completed and canceled work is excluded because
        this is a picture of who is currently carrying what, and a person who
        shipped four hundred issues last year is not carrying them.

        The unassigned pile is a real bucket and is kept. It is usually the
        largest one -- an issue nobody has picked up is the ordinary state of a
        new issue -- and dropping it would make the remaining bars add up to
        less than the board without saying so.

        `count(*) OVER ()` is evaluated after grouping, so it counts GROUPS:
        how many distinct assignees hold live work, which is exactly what the
        screen needs to say "20 of 34". A second statement would answer the
        same question from a second snapshot.

        `LEFT JOIN users` rather than `workspace_members`: the name is
        cosmetic, and an assignee is already guaranteed to be a member of this
        workspace by `issues_assignee_fk`, which is a composite key over
        (workspace_id, assignee_id). Joining membership again would re-check a
        constraint the database already holds.

        The tie-break is `assignee_id NULLS LAST`, so two people with the same
        count come back in the same order on every request. Without it the
        twentieth row is whichever the planner happened to emit, and "who is in
        the top twenty" would flicker between two identical requests.
        """
        rows = await connection.fetch(
            f"""
            SELECT
                issues.assignee_id AS assignee_id,
                COALESCE(NULLIF(btrim(users.name), ''), users.email) AS name,
                count(*) AS open_issues,
                count(*) OVER () AS assignee_total
            FROM issues
            {_STATE_JOIN}
            LEFT JOIN users ON users.id = issues.assignee_id
            WHERE issues.workspace_id = $1
                AND issues.archived_at IS NULL
                AND workflow_states.type <> ALL($2::TEXT[])
            GROUP BY issues.assignee_id, users.name, users.email
            ORDER BY count(*) DESC, issues.assignee_id NULLS LAST
            LIMIT $3
            """,
            scope.workspace_id,
            list(TERMINAL_STATE_CATEGORIES),
            limit,
        )

        loads = [
            AssigneeLoad(
                assignee_id=row["assignee_id"],
                name=row["name"],
                open_issues=row["open_issues"],
            )
            for row in rows
        ]

        # Zero groups means zero rows, so there is no window value to read and
        # the total is the only honest answer rather than a fallback.
        total = rows[0]["assignee_total"] if rows else 0

        return (loads, total)

    async def overdue(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        today: date,
    ) -> int:
        """Live, unfinished issues whose due date has already passed.

        `completed_at IS NULL` rather than a category join, and the two are the
        same question: the completed_at rule holds the column non-NULL if and
        only if the state is terminal. Asking it this way lets the statement be
        served entirely by `issues_workspace_live_due_date_id_idx` (015)
        without touching `workflow_states` at all.

        `due_date < $2` and never `<=`: a thing due today is not yet late.
        `due_date` is a DATE and `today` is a UTC calendar day, which is the
        same approximation `analytics_window` makes and the same one
        `app/repositories/reminders.py` already lives with -- a person in
        Auckland sees an issue turn red before their own day is over.
        """
        overdue: int = await connection.fetchval(
            """
            SELECT count(*)
            FROM issues
            WHERE workspace_id = $1
                AND archived_at IS NULL
                AND completed_at IS NULL
                AND due_date IS NOT NULL
                AND due_date < $2
            """,
            scope.workspace_id,
            today,
        )

        return overdue

    async def team_completion(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        start: datetime,
        end: datetime,
        limit: int,
    ) -> tuple[list[TeamCompletion], int]:
        """What each team delivered in the window, in that team's own unit.

        The estimate sum is per team and there is deliberately no workspace
        total -- `TeamCompletion` sets out why at length. The rule is enforced
        in the SQL rather than left to the caller: this statement cannot
        produce a cross-team sum, because `estimate_scale` is in the GROUP BY.

        `estimate_total` is NULL for a team on the t-shirt scale, because the
        stored integer there is a POSITION on a five-rung ladder rather than a
        quantity, and the sum of two positions is not a size. It is also NULL
        for a team that sized nothing, and the caller distinguishes the two by
        reading `estimated` -- which is why that count is returned beside it.

        `count(*) OVER ()` counts groups, as in `workload`: how many teams
        delivered anything at all, so the screen can say what the table omits.
        """
        rows = await connection.fetch(
            f"""
            SELECT
                teams.id AS team_id,
                teams.key AS key,
                teams.name AS name,
                teams.estimate_scale AS estimate_scale,
                count(*) AS completed,
                count(issues.estimate) AS estimated,
                CASE
                    WHEN teams.estimate_scale = 'tshirt' THEN NULL
                    ELSE sum(issues.estimate)
                END AS estimate_total,
                count(*) OVER () AS team_total
            FROM issues
            {_STATE_JOIN}
            JOIN teams
                ON teams.workspace_id = issues.workspace_id
                AND teams.id = issues.team_id
            WHERE issues.workspace_id = $1
                AND issues.completed_at >= $2
                AND issues.completed_at < $3
                AND workflow_states.type = 'completed'
            GROUP BY teams.id, teams.key, teams.name, teams.estimate_scale
            ORDER BY count(*) DESC, teams.key
            LIMIT $4
            """,
            scope.workspace_id,
            start,
            end,
            limit,
        )

        completions = [
            TeamCompletion(
                team_id=row["team_id"],
                key=row["key"],
                name=row["name"],
                estimate_scale=EstimateScale(row["estimate_scale"]),
                completed=row["completed"],
                estimated=row["estimated"],
                estimate_total=row["estimate_total"],
            )
            for row in rows
        ]

        return (completions, rows[0]["team_total"] if rows else 0)

    async def project_progress(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        limit: int,
    ) -> tuple[list[ProjectProgress], int]:
        """Live issues per project, and how many of them are done.

        One statement for two of the required metrics -- "issues by project"
        and "project progress" -- because they are the same two numbers read
        for different reasons, and splitting them would be two scans of the
        same rows returning a total that had to agree.

        No window. A project's progress is the whole project's, not the last
        thirty days' of it: a project that is 40% done is 40% done whether the
        reader asked about a fortnight or half a year.

        `count(*) FILTER` rather than a second query for the completed half, so
        the ratio's numerator and denominator come from one pass over one
        population and cannot be read either side of a concurrent write.

        Both counts exclude archived issues, so the ratio is over one
        population. Counting completions that had been filed away against a
        total that had not would make a project's progress FALL when somebody
        tidied up.

        Issues with no project are excluded entirely -- this is an inner join,
        and `project_id` is NULL on most issues. The unassigned pile is a real
        bucket in `workload` because a person is expected on an issue; a
        project is not, so "issues in no project" is the ordinary case rather
        than a gap, and a bar for it would be the largest one on every chart
        and would mean nothing.

        The tie-break is `projects.name` after the count, so two projects the
        same size come back in the same order on every request.
        """
        rows = await connection.fetch(
            f"""
            SELECT
                projects.id AS project_id,
                projects.name AS name,
                projects.state AS state,
                count(*) AS issues,
                count(*) FILTER (
                    WHERE workflow_states.type = 'completed'
                ) AS completed,
                count(*) OVER () AS project_total
            FROM issues
            {_STATE_JOIN}
            JOIN projects
                ON projects.workspace_id = issues.workspace_id
                AND projects.id = issues.project_id
            WHERE issues.workspace_id = $1
                AND issues.archived_at IS NULL
            GROUP BY projects.id, projects.name, projects.state
            ORDER BY count(*) DESC, projects.name
            LIMIT $2
            """,
            scope.workspace_id,
            limit,
        )

        progress = [
            ProjectProgress(
                project_id=row["project_id"],
                name=row["name"],
                state=row["state"],
                issues=row["issues"],
                completed=row["completed"],
            )
            for row in rows
        ]

        return (progress, rows[0]["project_total"] if rows else 0)

    async def cycle_progress(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        start: datetime,
        end: datetime,
        limit: int,
    ) -> tuple[list[CycleProgress], int]:
        """Scope and delivery for the cycles the window touches.

        Velocity and progress in one statement, for `ProjectProgress`'s reason:
        `completed` against `issues` is progress and `completed` against the
        cycle's length is velocity, and they are the same two numbers.

        WHICH CYCLES. The ones whose own span OVERLAPS the window --
        `starts_at < window_end AND ends_at >= window_start` -- rather than the
        N most recent. A cycle is a fixed period, so "does it overlap what the
        reader asked about" is a question with an exact answer, and the reader
        who narrows to seven days should stop seeing last quarter's sprints.
        The half-open comparison matches `window_bounds`.

        Note that this predicate is on the CYCLE's dates and not on
        `completed_at`, so a cycle's counts are over its whole scope rather
        than over the part of it that fell inside the window. That is the
        useful reading -- a sprint's progress is the sprint's, not the window's
        slice of it -- and it is the one the screen labels.

        The estimate sum is per cycle and carries the owning team's scale, for
        `TeamCompletion`'s reason. A cycle belongs to exactly one team
        (`cycles.team_id` is NOT NULL) so there is no mixed scale within a row;
        across rows there is, which is why nothing here adds two cycles up.

        The whole-cycle scope means an issue moved out of a cycle is simply
        gone from it: `issues.cycle_id` is where the issue is NOW, and this
        schema records `cycle_changed` in `issue_activity` but no estimate
        history, so the scope as it stood on a past day is not reconstructible.
        See the burndown note in `app.domain.analytics`.
        """
        rows = await connection.fetch(
            f"""
            SELECT
                cycles.id AS cycle_id,
                cycles.number AS number,
                cycles.name AS name,
                cycles.starts_at AS starts_at,
                cycles.ends_at AS ends_at,
                teams.key AS team_key,
                teams.estimate_scale AS estimate_scale,
                count(*) AS issues,
                count(*) FILTER (
                    WHERE workflow_states.type = 'completed'
                ) AS completed,
                count(issues.estimate) FILTER (
                    WHERE workflow_states.type = 'completed'
                ) AS estimated,
                CASE
                    WHEN teams.estimate_scale = 'tshirt' THEN NULL
                    ELSE sum(issues.estimate) FILTER (
                        WHERE workflow_states.type = 'completed'
                    )
                END AS completed_estimate,
                count(*) OVER () AS cycle_total
            FROM issues
            {_STATE_JOIN}
            JOIN cycles
                ON cycles.workspace_id = issues.workspace_id
                AND cycles.id = issues.cycle_id
            JOIN teams
                ON teams.workspace_id = issues.workspace_id
                AND teams.id = cycles.team_id
            WHERE issues.workspace_id = $1
                AND issues.archived_at IS NULL
                AND cycles.starts_at < $3
                AND cycles.ends_at >= $2
            GROUP BY
                cycles.id,
                cycles.number,
                cycles.name,
                cycles.starts_at,
                cycles.ends_at,
                teams.key,
                teams.estimate_scale
            ORDER BY cycles.starts_at DESC, teams.key
            LIMIT $4
            """,
            scope.workspace_id,
            start,
            end,
            limit,
        )

        progress = [
            CycleProgress(
                cycle_id=row["cycle_id"],
                number=row["number"],
                name=row["name"],
                starts_at=row["starts_at"],
                ends_at=row["ends_at"],
                team_key=row["team_key"],
                estimate_scale=EstimateScale(row["estimate_scale"]),
                issues=row["issues"],
                completed=row["completed"],
                estimated=row["estimated"],
                completed_estimate=row["completed_estimate"],
            )
            for row in rows
        ]

        return (progress, rows[0]["cycle_total"] if rows else 0)
