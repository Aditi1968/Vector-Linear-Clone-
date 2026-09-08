from uuid import UUID

import asyncpg

# The categories in which an issue is no longer being worked on. Imported
# rather than restated: it is the same rule `completed_at` follows and the same
# vocabulary migration 005's CHECK constrains, and a second copy here would be
# the one that stops matching the day a category is added.
from app.domain.issues import TERMINAL_STATE_CATEGORIES
from app.domain.reminders import DueIssue


class DueReminderRepository:
    """SQL over `issue_due_reminders`, which is a ledger and not a schedule.

    ONE STATEMENT, and the whole feature is in it. `claim_due` inserts a ledger
    row for every issue whose due date is inside the warning window and which
    has no row for that date yet, and returns the ones it wrote. Writing the
    record IS claiming the work, so there is no window between deciding to warn
    somebody and having recorded that they were warned.

    WHAT THAT MAKES IMPOSSIBLE, in the order it matters:

      * A SECOND REMINDER FOR THE SAME PROMISE. The primary key is (workspace,
        issue, due date), so a pass that runs every fifteen minutes writes the
        row on the first one and collides on every pass after it. That is
        `domain_events`' mechanism from migration 027 -- a natural key plus
        `ON CONFLICT DO NOTHING` -- applied to the same problem rather than a
        third one invented beside it.

      * TWO REPLICAS BOTH WARNING. Two processes running this statement in the
        same instant contend on that key: the second blocks on the first's
        uncommitted row, then re-evaluates and is returned nothing for it. So
        each issue is claimed by exactly one transaction, and the caller's
        fan-out runs inside that same transaction -- the ledger row and the
        inbox items commit together or neither does.

      * A REMINDER FOR AN ISSUE THAT IS ALREADY DONE. The select joins
        `workflow_states` and requires a non-terminal category, so an issue
        completed or cancelled before its due date arrives is never selected in
        the first place. That is a harder guarantee than filtering at delivery
        time would be, because there is no delivery time -- there is one
        statement.

    TENANCY, AND WHY THIS FILE LOOKS DIFFERENT FROM MOST REPOSITORIES. The one
    statement here carries no `workspace_id = $1`, deliberately: it is issued by
    a background sweep, which is the one reader in this system with no tenant.
    It serves no request, no caller's membership could scope it, and its whole
    job is to work through every workspace's calendar. That does not weaken
    isolation, because nothing it returns reaches a client -- the rows are
    consumed by `ScheduleWorker`, which turns each into a
    `NotificationRepository.notify_about_issue` call scoped by the
    `workspace_id` READ OFF THE ROW, never one supplied by a caller. The
    composite foreign key in migration 029 is what makes that read trustworthy,
    exactly as 028 argues for `embedding_jobs`.

    The repository receives a connection from the service layer. It never
    acquires connections, never touches the pool, and never owns a transaction.
    `asyncpg.Record` never escapes this class.
    """

    async def claim_due(
        self,
        connection: asyncpg.Connection,
        *,
        lead_days: int,
        limit: int,
    ) -> list[DueIssue]:
        """Record a reminder for every issue coming due, and say which.

        THE WINDOW IS `CURRENT_DATE` THROUGH `CURRENT_DATE + lead_days`, and it
        has a lower bound on purpose. An overdue issue is deliberately NOT
        reminded about:

          * a reminder is a WARNING, and a warning about something that has
            already happened is not one -- the overdue list is a filter, and
            `issues(filter: {due: OVERDUE})` is where somebody looks for it;
          * without the lower bound, the first pass after this feature deploys
            would fan out one notification for every overdue issue in the
            installation at once, and so would the first pass after any outage
            longer than a day. A warning system whose first act is a hundred
            notifications is one everybody mutes.

        The cost is that an outage longer than `lead_days` loses the warnings
        that fell inside it, which is one missed reminder rather than a
        backlog delivered late and out of context.

        `CURRENT_DATE` and not a date computed here, so that every replica --
        and the `due` filters on the read path -- agree about which day it is.
        migrations/029_estimates_dates.sql says which day that is: UTC, one
        clock, named.

        THE TERMINAL JOIN is `workflow_states.type <> ALL($terminal)`, using the
        categories and never the names -- 'Done' is a label a team may rename or
        delete, 'completed' is what migration 005's CHECK constrains. It is
        established INSIDE this statement rather than by a second read, which is
        the same move `EventRepository`'s `_COMPLETED_JOIN` makes: the statement
        that would write the row is the one that decides there is no row to
        write.

        `archived_at IS NULL` beside it, because an archived issue is absent
        from every other read in this product and must not reappear as a
        notification.

        `ORDER BY ... LIMIT` inside the CTE bounds one call, and the caller
        loops until a pass claims less than the batch. The order is by due date
        so that a backlog is worked soonest-first, and by id so it is total --
        which under contention is not a correctness matter but does make a claim
        reproducible for anybody debugging one.

        Returns what it INSERTED and not what it selected. Those differ by
        exactly the issues another replica claimed a moment earlier, which is
        the point.
        """
        rows = await connection.fetch(
            """
            WITH due AS (
                SELECT
                    issues.workspace_id,
                    issues.id AS issue_id,
                    issues.due_date
                FROM issues
                JOIN workflow_states
                    ON workflow_states.workspace_id = issues.workspace_id
                    AND workflow_states.id = issues.workflow_state_id
                LEFT JOIN issue_due_reminders AS reminded
                    ON reminded.workspace_id = issues.workspace_id
                    AND reminded.issue_id = issues.id
                    AND reminded.due_date = issues.due_date
                WHERE issues.archived_at IS NULL
                    AND issues.due_date IS NOT NULL
                    AND issues.due_date >= CURRENT_DATE
                    AND issues.due_date <= CURRENT_DATE + $1::int
                    AND workflow_states.type <> ALL($2::TEXT[])
                    AND reminded.issue_id IS NULL
                ORDER BY issues.due_date, issues.id
                LIMIT $3
            )
            INSERT INTO issue_due_reminders (workspace_id, issue_id, due_date)
            SELECT workspace_id, issue_id, due_date
            FROM due
            ON CONFLICT ON CONSTRAINT issue_due_reminders_pkey DO NOTHING
            RETURNING workspace_id, issue_id
            """,
            lead_days,
            list(TERMINAL_STATE_CATEGORIES),
            limit,
        )

        return [
            DueIssue(workspace_id=row["workspace_id"], issue_id=row["issue_id"])
            for row in rows
        ]

    async def has_been_reminded(
        self,
        connection: asyncpg.Connection,
        *,
        workspace_id: UUID,
        issue_id: UUID,
    ) -> bool:
        """Whether this issue has ever been warned about any due date.

        A seek on the primary key's leading two columns, for tests and for
        whoever is answering "was I told about this" from a support ticket.
        Nothing on the request path selects it: the ledger is bookkeeping, and
        what a person sees is the notification it produced.
        """
        found = await connection.fetchval(
            """
            SELECT 1
            FROM issue_due_reminders
            WHERE workspace_id = $1 AND issue_id = $2
            LIMIT 1
            """,
            workspace_id,
            issue_id,
        )

        return found is not None
