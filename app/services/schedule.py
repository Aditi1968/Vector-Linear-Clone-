"""The calendar half of the product, run by the process that serves it.

Two jobs, one loop, and they are together because they are the same job:
something that a DATE makes due, swept for by a process with no request behind
it. A due date coming up has to reach the people it concerns; a recurrence
reaching its day has to file an issue. Neither has a caller, both key on
`CURRENT_DATE`, and running them from two tasks would be two sleeps, two
supervisors and two things to start.

THERE IS NO SCHEDULER, and this is the honest replacement rather than a
stand-in for a missing dependency -- the same argument `app.main` already makes
for the embedding worker and `run_delivery_loop`. The options were a cron
container, a queue broker, or a task on the process that is already running,
and the first two are infrastructure a deployment has to operate, bought to
call a coroutine every quarter of an hour.

EXACTLY ONCE UNDER TWO REPLICAS, twice over, by two mechanisms that already
exist in this repository rather than a third invented here:

  * REMINDERS claim by WRITING THE LEDGER ROW. `issue_due_reminders_pkey` is
    (workspace, issue, due date) and the sweep's statement is one
    `INSERT ... SELECT ... ON CONFLICT DO NOTHING RETURNING`, so the work a pass
    takes is exactly the rows its own INSERT returned. A second replica in the
    same instant blocks on the first's uncommitted key and is returned nothing.
    That is `domain_events`' dedupe key from migration 027, applied to the same
    problem. The fan-out runs inside the SAME transaction, so the ledger row and
    the inbox items commit together -- there is no window in which somebody was
    recorded as told and was not.

  * RECURRENCES claim by MOVING `next_run_on`. `SELECT ... FOR UPDATE SKIP
    LOCKED` then `UPDATE`, in one transaction, which is exactly
    `EventRepository.claim_next`'s shape: two sweeps take disjoint sets because
    locked rows are skipped rather than waited on, and whichever commits first
    has moved the date past today, so the other's predicate no longer matches
    the row.

THE ONE PLACE THIS IS NOT ATOMIC is the recurrence half, and it is a deliberate
trade stated rather than discovered. The claim commits BEFORE the issue is
filed, because filing goes through `TemplateService.apply`, which owns
transactions of its own -- correctly, since every rule an apply must respect
lives in `IssueService` and `LabelService`, and reaching past them would grow a
second `issueCreate` nobody would keep in step. So a process that dies between
the two loses ONE occurrence. The alternative -- advance after filing -- means a
crash re-files the same issue on the next pass, and a duplicate "Weekly
standup" appearing in a board is a worse outcome than a missing one, because
nobody can tell which of the two is the real one. The same accounting
`SlackNotifier` keeps when it counts an attempt at claim time.
"""

import asyncio
from datetime import date
from uuid import UUID

import asyncpg
import structlog

from app.domain.notifications import NotificationKind
from app.domain.recurrence import (
    RecurrenceEntity,
    due_date_for,
    next_occurrence,
)
from app.domain.tenancy import WorkspaceScope
from app.repositories.notifications import NotificationRepository
from app.repositories.recurrences import RecurrenceRepository
from app.repositories.reminders import DueReminderRepository
from app.services.templates import TemplateService


logger = structlog.get_logger(__name__)


# How many days before an issue is due somebody hears about it.
#
# One. A due date in this product is a calendar day, so the useful warning is
# "this is due tomorrow" -- a person can act on that before the day arrives,
# which is the whole point, and two days out is far enough away to be ignored
# and then forgotten.
#
# It is deliberately not a per-workspace setting. A number every workspace would
# leave at its default is a settings screen, a column, a migration and a
# validation rule bought to change one integer, and there is no evidence yet
# that two workspaces want different ones. The upgrade path is a column on
# `workspaces` read into the statement's `$1`, which is already a parameter for
# exactly that reason.
REMINDER_LEAD_DAYS = 1

# How many issues one reminder claim takes at a time.
#
# The bound that matters is the fan-out: `notify_about_issue` is one statement
# per issue, inside the claiming transaction, so this is how many statements one
# transaction holds. 200 local inserts is milliseconds, and the loop goes round
# until a pass claims less than a full batch -- so a workspace with a thousand
# deadlines on one day is drained by five passes rather than by one transaction
# holding a connection while it works.
REMINDER_BATCH = 200

# How many schedules one recurrence claim takes at a time.
#
# Much smaller than the reminder batch, and for a reason that is not about this
# statement: each claimed row becomes a `TemplateService.apply`, which allocates
# an issue number and therefore takes a row lock on the TEAM held to the end of
# its own transaction. Twenty-five is a batch that finishes quickly enough not
# to be noticed by somebody filing an issue on the same team, and a crash
# forfeits at most twenty-five occurrences.
RECURRENCE_BATCH = 25

# How long the loop waits between passes.
#
# Both halves key on a calendar DAY, so the granularity that matters is
# "sometime today" and an hour would do. Fifteen minutes is chosen for the two
# edges an hour handles badly: a due date set late in the evening still produces
# its reminder before the day turns over, and a recurrence saved to start today
# files its first issue while the person who saved it is still looking at the
# screen. Running more often costs nothing -- the ledger key and the advanced
# date make every extra pass a no-op.
POLL_INTERVAL_SECONDS = 900.0


class ScheduleWorker:
    """The pass that turns dates into notifications and issues.

    Holds a pool like every other service and owns its own transaction
    boundaries.

    TAKES NO SCOPE ON ANY METHOD, and that absence needs stating rather than
    passing unnoticed -- it is the same one `SlackNotifier` documents. There is
    no viewer here: this runs on no request, on behalf of nobody, from a
    background loop. What stands in for an authorization check is that the
    workspace is never supplied. It arrives on the row the database matched, and
    everything done with it is keyed on THAT id: the notification fan-out, the
    template read, the issue insert. There is no argument anywhere in this class
    through which a caller could aim one workspace's schedule at another
    workspace's board, and migration 029's composite foreign keys are the floor
    under it -- a recurrence pairing one tenant's template with another's team
    is not a row that exists to be claimed.

    A bare `WorkspaceScope` and never an `AuthorizedWorkspaceScope`, for the
    reason `EmbeddingWorker._store` gives: constructing the second would be a
    lie about what was checked, because nothing was.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        reminders: DueReminderRepository,
        notifications: NotificationRepository,
        recurrences: RecurrenceRepository,
        templates: TemplateService,
    ):
        self._pool = pool
        self._reminders = reminders

        # The repository and not `ActivityService`: every notification method
        # on that class takes an `AuthorizedWorkspaceScope` -- correctly,
        # because they are things a person does to their own inbox -- and this
        # loop has no person to build one from. The same reach `SlackNotifier`
        # makes for `SlackRepository`.
        self._notifications = notifications

        self._recurrences = recurrences

        # A SERVICE here, unlike the two repositories above, and the asymmetry
        # is the point. Filing an issue from a template is four writes across
        # two services and every rule they enforce -- which state a new issue
        # starts in, which number it gets, whether the estimate fits the team's
        # scale, how many labels one may wear -- has to apply to an issue this
        # loop files exactly as it does to one a person files. Reaching for the
        # repositories would be this file quietly growing a second
        # `issueCreate` that nobody would think to keep in step.
        self._templates = templates

    async def run_once(self) -> tuple[int, int]:
        """One pass: warn about what is coming due, file what is scheduled.

        Returns (reminded, filed), which is what a test asserts on and what the
        log line below reports. Two numbers rather than a sum, because they are
        two different things going right and a caller reading "7" could not tell
        a busy calendar from a busy schedule.

        The two halves are independent and neither is inside the other's
        transaction: a recurrence that cannot be filed must not roll back
        reminders that were already delivered to inboxes.
        """
        reminded = await self._remind()
        filed = await self._generate()

        if reminded or filed:
            logger.info("schedule.pass", reminded=reminded, filed=filed)

        return reminded, filed

    async def run_forever(self) -> None:
        """Pass after pass, until the task is cancelled.

        THE WHOLE SCHEDULER, and it is a loop and a sleep on purpose; see the
        module docstring.

        The sleep is AFTER the pass and outside the try, so it happens on every
        path including the one where `run_once` raises on its first statement.
        That ordering is the loop's half of "cannot hot-loop": a database
        refusing every connection would otherwise be a tight loop of failing
        connects with a log line each. The claim statements are the other half,
        and they need no help from this frame -- a reminder already written and
        a schedule already advanced stay that way whatever this loop does.

        `except Exception` is deliberate and joins the two other places in this
        codebase that catch broadly, for the same reason `EmbeddingWorker`
        gives: a supervisor loop that propagates has no supervisor above it, so
        the task ends, nothing restarts it, and the calendar silently stops
        working for the life of the process -- which is the exact failure this
        class exists to end. Discovering that from somebody asking why nobody
        was reminded is discovering it far too late.

        `asyncio.CancelledError` derives from BaseException and so is NOT caught
        by that clause. That is what makes shutdown work: the lifespan cancels
        this task, the cancellation travels out of `asyncio.sleep`, and the loop
        ends rather than logging its own shutdown as a failure and going round
        again.
        """
        while True:
            try:
                await self.run_once()
            except Exception:
                logger.exception("schedule.pass_failed")

            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    async def _remind(self) -> int:
        """Warn everyone concerned about every issue coming due; count them.

        THE CLAIM AND THE FAN-OUT ARE ONE TRANSACTION, which is what makes a
        reminder exactly-once rather than at-most-once or at-least-once. The
        ledger row is the claim, so if the notifications fail the row rolls back
        with them and the next pass tries again; if the notifications land the
        row lands too, and no later pass will select that issue for that date.

        Recipients are not chosen here and there is no parameter for one:
        `notify_about_issue` derives them from the issue's own row and from
        `issue_subscribers`, in the statement, so a service cannot pass somebody
        it invented and the removed-member exclusion migration 026 added cannot
        be skipped by a caller that forgot.

        `actor_id=None`, because nobody did this. A date arrived. That is also
        why nobody is excluded from their own reminder -- the assignee of an
        issue coming due is precisely who the reminder is for, and
        `IS DISTINCT FROM NULL` keeps every recipient.

        `include_creator=True`, which is the one flag this call has and the one
        judgement worth arguing. A due date is usually set by whoever planned
        the work rather than by whoever is doing it, and an UNASSIGNED issue
        coming due with nobody told is exactly the failure this feature exists
        to prevent -- under `include_creator=False` such an issue would notify
        only its watchers, and an issue nobody has picked up rarely has any.

        Loops until a pass claims less than a full batch, so a day with more
        deadlines than one batch is drained now rather than a batch every
        quarter of an hour. It terminates because every claimed row is written
        into the ledger and the next select's anti-join excludes it.
        """
        total = 0

        while True:
            async with self._pool.acquire() as connection:
                async with connection.transaction():
                    claimed = await self._reminders.claim_due(
                        connection,
                        lead_days=REMINDER_LEAD_DAYS,
                        limit=REMINDER_BATCH,
                    )

                    for due in claimed:
                        await self._notifications.notify_about_issue(
                            connection,
                            # Built from the CLAIMED ROW and from nothing else.
                            # A worker has no viewer and no membership, so there
                            # is no `AuthorizedWorkspaceScope` to be had;
                            # migration 029's composite foreign key is what
                            # makes this read trustworthy.
                            scope=WorkspaceScope(workspace_id=due.workspace_id),
                            issue_id=due.issue_id,
                            actor_id=None,
                            kind=NotificationKind.DUE_SOON,
                            include_creator=True,
                        )

            total += len(claimed)

            if len(claimed) < REMINDER_BATCH:
                return total

    async def _generate(self) -> int:
        """File one issue for every schedule due today; count what landed.

        TWO PHASES, AND THE CLAIM COMMITS FIRST. The lock-and-advance is one
        short transaction; the applies run after it, each with transactions of
        their own. See the module docstring for why that ordering is the right
        one and what it costs.

        `today` comes back from the claim rather than from this process's clock,
        so the advance is computed from the same day the predicate selected on
        -- a replica whose container clock drifted, or one that crossed midnight
        between the statement and this line, cannot advance a schedule past a
        day it never claimed.

        ONE OCCURRENCE PER PASS, EVEN AFTER AN OUTAGE. The advance is
        `next_occurrence(after=today)`, so a weekly schedule that was due three
        weeks ago files one issue and moves to the next occurrence after today
        rather than backfilling three identical ones. An obligation for a week
        that has gone is not something an issue filed now can discharge, and
        three copies of "weekly report" is the shape somebody comes back from
        holiday to and archives in bulk.

        THIS WEEK'S ISSUE APPEARS WHETHER OR NOT LAST WEEK'S WAS CLOSED, and
        that is a product decision rather than an oversight. A recurrence
        describes a calendar obligation, and Monday arrives whether or not last
        Monday's got done; suppressing the new one would make a missed week
        silently vanish, so a person returning to three open standups would see
        one and have no record of the other two. The two instances are told
        apart by their due dates.

        ponytail: no `skip_when_open`. A workspace that wants the other
        behaviour gets one BOOLEAN column and one EXISTS probe against the
        previous instance -- which needs a `last_issue_id` on the row to probe,
        so it is a column and a foreign key, not a flag. Buy it when somebody
        asks, not before.

        A FAILED APPLY IS LOGGED AND THE PASS CONTINUES. The schedule has
        already advanced, so this occurrence is lost rather than retried -- and
        the remaining claims are other workspaces' work, which one workspace's
        deleted project must not stop. `except Exception` here for the reason
        `EmbeddingWorker._embed` gives about a poison row: a template that
        cannot be applied has to be RECORDED and stepped over, not allowed to
        end the pass.
        """
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                today, claimed = await self._recurrences.lock_due(
                    connection,
                    limit=RECURRENCE_BATCH,
                )

                for workspace_id, recurrence in claimed:
                    await self._recurrences.advance(
                        connection,
                        workspace_id=workspace_id,
                        template_id=recurrence.template_id,
                        next_run_on=next_occurrence(recurrence.rule, after=today),
                    )

        filed = 0

        for workspace_id, recurrence in claimed:
            if await self._file(workspace_id, recurrence, today):
                filed += 1

        return filed

    async def _file(
        self,
        workspace_id: UUID,
        recurrence: RecurrenceEntity,
        today: date,
    ) -> bool:
        """File one recurring issue, or log why it did not happen.

        Separated from `_generate` so the broad `except` wraps exactly one
        apply and cannot swallow anything else in the loop around it.

        The scope is built from the claimed row's `workspace_id`, and the
        template and team come off the same row -- so the tenant, the shape and
        the destination all arrive from one place the database matched together.

        `actor_id=None`: a recurring issue has no author. `issues.creator_id` is
        nullable for exactly this kind of case -- migration 006 defines it as
        "no known creator", which an issue nobody filed is -- and inventing one
        would mean naming a person as the author of something they did not do,
        and then notifying everybody else about it on their behalf.

        The title is the template's own, and there is deliberately no
        date-stamping of it. "Weekly report" filed four times is four issues
        with the same title and four different due dates, which is what the
        board already renders; interpolating the date would be this module
        deciding a naming convention for every workspace.
        """
        try:
            await self._templates.apply(
                scope=WorkspaceScope(workspace_id=workspace_id),
                template_id=recurrence.template_id,
                team_id=recurrence.team_id,
                due_date=due_date_for(recurrence.rule, filed_on=today),
                actor_id=None,
            )
        except Exception:
            logger.exception(
                "schedule.recurrence_failed",
                workspace_id=str(workspace_id),
                template_id=str(recurrence.template_id),
            )

            return False

        return True
