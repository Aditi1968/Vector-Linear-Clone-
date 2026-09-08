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
    CycleTimeSummary,
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

    async def completion_series(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        start: datetime,
        end: datetime,
        range_start: date,
        days: int,
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
            range_start,
            days,
        )

    async def cycle_time(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        start: datetime,
        end: datetime,
    ) -> CycleTimeSummary | None:
        """Median and p90 hours from creation to completion, or nothing.

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

        return CycleTimeSummary(
            count=row["sampled"],
            median_hours=row["median_seconds"] / _SECONDS_PER_HOUR,
            p90_hours=row["p90_seconds"] / _SECONDS_PER_HOUR,
        )

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
