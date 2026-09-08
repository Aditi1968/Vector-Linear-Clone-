"""The sweep that turns dates into notifications and issues, above the database.

Two halves with two claim mechanisms, and this file is about the code around
them. The mechanisms themselves are properties of PostgreSQL and are exercised
in tests/test_migration_029_db.py, which holds one transaction open while
another claims; what is checked here is everything the SQL cannot enforce:

  * WHO IS REMINDED, and that the sweep does not choose. Recipients come from
    `notify_about_issue`, which derives them in its own statement, and the
    worker passes no recipient because there is no parameter for one.
  * THAT THE FAN-OUT IS INSIDE THE CLAIMING TRANSACTION. The ledger row is the
    claim, so a notification that fails must take the row with it -- otherwise
    the issue is recorded as warned and nobody was.
  * THE TENANT COMES OFF THE ROW. The worker has no viewer, so every scope it
    builds has to be the `workspace_id` the database matched.
  * THE ADVANCE IS COMPUTED FROM THE DATABASE'S TODAY, not the process clock.
  * ONE BAD TEMPLATE DOES NOT STOP THE PASS, and its occurrence is not retried
    -- the schedule has already moved.

No `db` mark: nothing here reaches PostgreSQL.
"""

from datetime import date, timedelta
from uuid import UUID

import pytest

from app.domain.notifications import NotificationKind
from app.domain.recurrence import RecurrenceEntity, RecurrenceFrequency
from app.domain.reminders import DueIssue
from app.domain.tenancy import WorkspaceScope
from app.services.schedule import (
    RECURRENCE_BATCH,
    REMINDER_BATCH,
    REMINDER_LEAD_DAYS,
    ScheduleWorker,
)

from tests.conftest import FakePool


WORKSPACE_A = UUID("00000000-0000-7000-8000-0000000000a1")
WORKSPACE_B = UUID("00000000-0000-7000-8000-0000000000b1")
ISSUE_ONE = UUID("00000000-0000-7000-8000-000000000001")
ISSUE_TWO = UUID("00000000-0000-7000-8000-000000000002")
TEMPLATE_ID = UUID("00000000-0000-7000-8000-00000000000a")
TEAM_ID = UUID("00000000-0000-7000-8000-00000000000b")

TODAY = date(2026, 3, 2)  # a Monday


def recurrence(
    *,
    template_id: UUID = TEMPLATE_ID,
    team_id: UUID = TEAM_ID,
    weekdays: tuple[int, ...] = (1,),
    due_in_days: int | None = None,
    next_run_on: date = TODAY,
) -> RecurrenceEntity:
    return RecurrenceEntity(
        template_id=template_id,
        team_id=team_id,
        frequency=RecurrenceFrequency.WEEKLY,
        interval_count=1,
        weekdays=weekdays,
        day_of_month=None,
        starts_on=TODAY - timedelta(days=28),
        due_in_days=due_in_days,
        next_run_on=next_run_on,
    )


class FakeReminders:
    """Hands back batches of claimed issues, one per `claim_due` call."""

    def __init__(self, batches: list[list[DueIssue]] | None = None):
        self.batches = list(batches) if batches is not None else [[]]
        self.calls: list[dict] = []

    async def claim_due(self, connection, *, lead_days, limit):
        self.calls.append({"lead_days": lead_days, "limit": limit})

        if not self.batches:
            return []

        return self.batches.pop(0)


class FakeNotifications:
    def __init__(self, fail_on: UUID | None = None):
        self.fail_on = fail_on
        self.calls: list[dict] = []

    async def notify_about_issue(
        self, connection, *, scope, issue_id, actor_id, kind, include_creator
    ):
        self.calls.append(
            {
                "scope": scope,
                "issue_id": issue_id,
                "actor_id": actor_id,
                "kind": kind,
                "include_creator": include_creator,
            }
        )

        if issue_id == self.fail_on:
            raise RuntimeError("the inbox write failed")


class FakeRecurrences:
    def __init__(self, due=None, today: date = TODAY):
        self.due = due if due is not None else []
        self.today = today
        self.locks: list[dict] = []
        self.advances: list[dict] = []

    async def lock_due(self, connection, *, limit):
        self.locks.append({"limit": limit})

        return self.today, list(self.due)

    async def advance(self, connection, *, workspace_id, template_id, next_run_on):
        self.advances.append(
            {
                "workspace_id": workspace_id,
                "template_id": template_id,
                "next_run_on": next_run_on,
            }
        )


class FakeTemplates:
    def __init__(self, fail_on: UUID | None = None):
        self.fail_on = fail_on
        self.applied: list[dict] = []

    async def apply(self, *, scope, template_id, team_id, due_date, actor_id):
        self.applied.append(
            {
                "scope": scope,
                "template_id": template_id,
                "team_id": team_id,
                "due_date": due_date,
                "actor_id": actor_id,
            }
        )

        if template_id == self.fail_on:
            raise RuntimeError("the project this template names was deleted")


def worker(
    *,
    reminders=None,
    notifications=None,
    recurrences=None,
    templates=None,
) -> ScheduleWorker:
    return ScheduleWorker(
        pool=FakePool(),
        reminders=reminders if reminders is not None else FakeReminders(),
        notifications=(
            notifications if notifications is not None else FakeNotifications()
        ),
        recurrences=recurrences if recurrences is not None else FakeRecurrences(),
        templates=templates if templates is not None else FakeTemplates(),
    )


# --- reminders --------------------------------------------------------


async def test_every_claimed_issue_is_fanned_out_under_its_own_workspace():
    """The tenancy property, over two workspaces in one batch.

    The sweep claims across every workspace at once -- it has no tenant of its
    own -- so "workspace A's reminder reached A's members" cannot be a claim
    about a run that only ever saw A. Each scope has to be built from the id on
    its own row.
    """
    notifications = FakeNotifications()

    await worker(
        reminders=FakeReminders(
            [
                [
                    DueIssue(workspace_id=WORKSPACE_A, issue_id=ISSUE_ONE),
                    DueIssue(workspace_id=WORKSPACE_B, issue_id=ISSUE_TWO),
                ],
                [],
            ]
        ),
        notifications=notifications,
    )._remind()

    assert [call["scope"] for call in notifications.calls] == [
        WorkspaceScope(workspace_id=WORKSPACE_A),
        WorkspaceScope(workspace_id=WORKSPACE_B),
    ]
    assert [call["issue_id"] for call in notifications.calls] == [ISSUE_ONE, ISSUE_TWO]


async def test_a_reminder_has_no_actor_and_reaches_the_creator():
    """The two judgements the fan-out call makes, pinned.

    No actor, because nobody did this -- a date arrived. That is also what
    keeps the assignee in the recipient list: `notify_about_issue` excludes the
    actor, and excluding nobody is what `None` means there.

    The creator IS included, and it is the one flag worth arguing. A due date is
    usually set by whoever planned the work, and an unassigned issue coming due
    with nobody told is exactly the failure this feature exists to prevent --
    under `include_creator=False` such an issue would notify only its watchers,
    and an issue nobody has picked up rarely has any.
    """
    notifications = FakeNotifications()

    await worker(
        reminders=FakeReminders(
            [[DueIssue(workspace_id=WORKSPACE_A, issue_id=ISSUE_ONE)], []]
        ),
        notifications=notifications,
    )._remind()

    assert notifications.calls == [
        {
            "scope": WorkspaceScope(workspace_id=WORKSPACE_A),
            "issue_id": ISSUE_ONE,
            "actor_id": None,
            "kind": NotificationKind.DUE_SOON,
            "include_creator": True,
        }
    ]


async def test_the_claim_asks_for_the_lead_time_and_the_batch_the_module_declares():
    reminders = FakeReminders()

    await worker(reminders=reminders)._remind()

    assert reminders.calls == [
        {"lead_days": REMINDER_LEAD_DAYS, "limit": REMINDER_BATCH}
    ]


async def test_a_full_batch_is_followed_by_another_pass_and_a_short_one_is_not():
    """A day with more deadlines than one batch is drained now.

    The loop terminates because every claimed row is written into the ledger and
    the next select's anti-join excludes it -- so the fake's second batch being
    short is what a real second claim would answer.
    """
    full = [
        DueIssue(workspace_id=WORKSPACE_A, issue_id=UUID(int=index))
        for index in range(REMINDER_BATCH)
    ]
    reminders = FakeReminders([full, [DueIssue(WORKSPACE_A, ISSUE_ONE)]])

    reminded = await worker(reminders=reminders)._remind()

    assert reminded == REMINDER_BATCH + 1
    assert len(reminders.calls) == 2


async def test_a_failed_fan_out_takes_the_claim_with_it():
    """The ledger row is the claim, so it must not survive a failed inbox write.

    A real transaction is what enforces this -- the claim and the fan-out are
    one `async with connection.transaction()` -- and what is asserted here is
    the half a fake can see: the exception is not swallowed. A worker that
    caught it would leave the issue recorded as warned with nobody warned, and
    no later pass would ever select it again.
    """
    reminders = FakeReminders(
        [
            [
                DueIssue(workspace_id=WORKSPACE_A, issue_id=ISSUE_ONE),
                DueIssue(workspace_id=WORKSPACE_A, issue_id=ISSUE_TWO),
            ]
        ]
    )

    with pytest.raises(RuntimeError):
        await worker(
            reminders=reminders,
            notifications=FakeNotifications(fail_on=ISSUE_TWO),
        )._remind()


# --- recurrences ------------------------------------------------------


async def test_the_advance_is_computed_from_the_databases_today():
    """Not from this process's clock, and the fake's `today` proves which.

    `TODAY` here is a date the machine running the suite is nowhere near, so an
    implementation reading `date.today()` would advance to a different Monday
    and this assertion would fail rather than passing by coincidence on the
    day somebody ran it.
    """
    recurrences = FakeRecurrences(due=[(WORKSPACE_A, recurrence())])

    await worker(recurrences=recurrences)._generate()

    assert recurrences.advances == [
        {
            "workspace_id": WORKSPACE_A,
            "template_id": TEMPLATE_ID,
            "next_run_on": TODAY + timedelta(days=7),
        }
    ]


async def test_a_schedule_stuck_in_the_past_advances_past_today_and_files_once():
    """Three weeks of downtime is ONE issue, not three.

    The catch-up rule, asserted through the worker rather than only through the
    arithmetic: the advance is `next_occurrence(after=today)`, so the missed
    Mondays are gone rather than queued.
    """
    stale = recurrence(next_run_on=TODAY - timedelta(days=21))
    recurrences = FakeRecurrences(due=[(WORKSPACE_A, stale)])
    templates = FakeTemplates()

    filed = await worker(recurrences=recurrences, templates=templates)._generate()

    assert filed == 1
    assert len(templates.applied) == 1
    assert recurrences.advances[0]["next_run_on"] == TODAY + timedelta(days=7)


async def test_the_issue_is_filed_under_the_workspace_and_team_on_the_row():
    """Every value in the apply comes off the claimed row, none from anywhere
    else -- the worker has no caller to take a workspace or a team from."""
    templates = FakeTemplates()

    await worker(
        recurrences=FakeRecurrences(
            due=[(WORKSPACE_B, recurrence(due_in_days=4))],
        ),
        templates=templates,
    )._generate()

    assert templates.applied == [
        {
            "scope": WorkspaceScope(workspace_id=WORKSPACE_B),
            "template_id": TEMPLATE_ID,
            "team_id": TEAM_ID,
            "due_date": TODAY + timedelta(days=4),
            # A recurring issue has no author. Inventing one would name a
            # person as the author of something they did not do, and then
            # notify everybody else about it on their behalf.
            "actor_id": None,
        }
    ]


async def test_one_failing_template_does_not_stop_the_others():
    """A poison row is recorded and stepped over, not allowed to end the pass.

    The remaining claims are other workspaces' work, and one workspace's
    deleted project must not stop it. The count reports the truth -- one filed,
    not two -- so a caller reading it is not told the pass went better than it
    did.
    """
    templates = FakeTemplates(fail_on=TEMPLATE_ID)
    other = UUID("00000000-0000-7000-8000-00000000000c")

    filed = await worker(
        recurrences=FakeRecurrences(
            due=[
                (WORKSPACE_A, recurrence()),
                (WORKSPACE_B, recurrence(template_id=other)),
            ],
        ),
        templates=templates,
    )._generate()

    assert filed == 1
    assert [call["template_id"] for call in templates.applied] == [TEMPLATE_ID, other]


async def test_a_failed_apply_does_not_put_the_occurrence_back():
    """The trade this design makes, stated as an assertion.

    The claim commits before the apply, so a failure loses ONE occurrence
    rather than retrying it. That is deliberate: retrying means a crash between
    the two re-files the same issue on the next pass, and a duplicate "Weekly
    standup" in a board is worse than a missing one, because nobody can tell
    which of the two is the real one.
    """
    recurrences = FakeRecurrences(due=[(WORKSPACE_A, recurrence())])

    await worker(
        recurrences=recurrences,
        templates=FakeTemplates(fail_on=TEMPLATE_ID),
    )._generate()

    assert recurrences.advances[0]["next_run_on"] == TODAY + timedelta(days=7)


async def test_the_claim_asks_for_the_batch_the_module_declares():
    """Small on purpose: each claimed row becomes an apply that takes a row
    lock on the TEAM until its own transaction ends."""
    recurrences = FakeRecurrences()

    await worker(recurrences=recurrences)._generate()

    assert recurrences.locks == [{"limit": RECURRENCE_BATCH}]
    assert RECURRENCE_BATCH < REMINDER_BATCH


# --- the pass as a whole ----------------------------------------------


async def test_one_pass_runs_both_halves_and_reports_each_separately():
    """Two numbers rather than a sum: a caller reading "2" could not tell a
    busy calendar from a busy schedule."""
    reminded, filed = await worker(
        reminders=FakeReminders(
            [[DueIssue(workspace_id=WORKSPACE_A, issue_id=ISSUE_ONE)], []]
        ),
        recurrences=FakeRecurrences(due=[(WORKSPACE_A, recurrence())]),
    ).run_once()

    assert (reminded, filed) == (1, 1)


async def test_an_empty_calendar_is_a_pass_that_does_nothing():
    assert await worker().run_once() == (0, 0)
