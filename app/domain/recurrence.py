"""When a recurring issue is next filed.

Pure application code -- no Strawberry, FastAPI, asyncpg or PostgreSQL, and no
clock. Everything here is `datetime.date` arithmetic over values migration 029
stores as DATE.

THE DST ANSWER IS THAT THERE IS NOTHING FOR DST TO MOVE. A daylight-saving
transition shifts INSTANTS -- it makes a day 23 or 25 hours long -- and this
module never holds one. "Every Tuesday" is a property of the calendar, and the
calendar has a Tuesday in the week the clocks change exactly as it does in
every other week. The single place a clock enters the feature is the database
deciding what `CURRENT_DATE` is, which migration 029 documents as UTC and which
the due-date filters and the reminder sweep answer identically -- so nothing in
the product can disagree with anything else in it about which day today is.
This is 006's "the day something is expected is a DATE" paying off.

THE ARITHMETIC IS A FORWARD SCAN, deliberately. `next_occurrence` walks days
one at a time and asks `occurs_on` about each. The alternative -- closed-form
month arithmetic with a clamp and an interval stride -- is four lines shorter
and is where every bug in this kind of code lives: off-by-one on the stride,
February clamping that then drifts, a leap year, a weekday set that wraps a
week boundary. A predicate that says exactly what a day has to satisfy is
reviewable by reading it, and the scan is bounded by `MAX_SCAN_DAYS` and runs
once per issue generated. There is no volume here to optimise for.
"""

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from typing import Final
from uuid import UUID


class RecurrenceFrequency(StrEnum):
    """How often a template files itself.

    Three, and not a cron expression. migrations/029_estimates_dates.sql argues
    that at length: a cron field is a small language with its own parser and its
    own surprises, bought so a project tracker can express a schedule nobody has
    ever asked a project tracker for. These three with an interval cover a daily
    checklist, a weekly report, a fortnightly retro and a monthly invoice.

    A StrEnum because the member IS the stored spelling, so nothing converts at
    the repository boundary and a frequency cannot reach the database in a
    casing `issue_recurrences_frequency_known` refuses.
    """

    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


# How far `next_occurrence` will look before giving up.
#
# The furthest legitimate gap is a monthly recurrence at the maximum interval:
# 52 months of at most 31 days, plus one to clear the starting day. Every rule
# the CHECK constraints admit has an occurrence inside that window -- monthly
# clamps rather than skipping, so every month qualifies, and weekly always has
# at least one weekday in its set -- so reaching this bound means the rule is
# one the constraints should have refused.
MAX_SCAN_DAYS: Final = 52 * 31 + 31


class UnreachableRecurrenceError(Exception):
    """A rule with no occurrence inside `MAX_SCAN_DAYS`.

    Deliberately NOT a ValidationError. Every rule that reaches this module has
    already been through `issue_recurrences_frequency_known` and its four
    sibling CHECKs, each of which guarantees an occurrence exists -- so a rule
    that fails here is not input a client can correct, it is a row written by
    something that did not go through them, or a defect in `occurs_on`.
    Reporting it as a field error would tell somebody to fix a request that was
    never the problem, and returning a date anyway would file an issue on a day
    the schedule does not name.
    """

    def __init__(self) -> None:
        super().__init__("Recurrence rule has no occurrence in range")


@dataclass(frozen=True, slots=True)
class RecurrenceRule:
    """The schedule itself, without the row it is stored in.

    Separated from `RecurrenceEntity` below for the reason
    `IssueTemplateDraft` is separated from `IssueTemplateEntity`: this is what a
    caller SUPPLIES and what the arithmetic reads, so neither `next_occurrence`
    nor a save has to be handed a `next_run_on` it is about to compute, nor a
    `template_id` it has no use for.

    `weekdays` is a tuple of ISO weekday numbers (1 = Monday, 7 = Sunday), empty
    for the two frequencies that do not use it -- and never None, so `in` works
    without a guard. `day_of_month` is None for the same two.
    `issue_recurrences_weekdays_match_frequency` and its sibling make those
    equivalences structural rather than conventional; `is_consistent` below is
    this layer's copy, so a rule can be refused with a field error before a
    connection is taken rather than as a masked CHECK violation.

    `starts_on` is the anchor AND the "not before" bound, and it has to be a
    stored value rather than something derived: "every two weeks on Tuesday"
    does not say which Tuesdays without a week to count from.
    """

    frequency: RecurrenceFrequency
    interval_count: int
    starts_on: date
    weekdays: tuple[int, ...] = ()
    day_of_month: int | None = None
    due_in_days: int | None = None

    @property
    def is_consistent(self) -> bool:
        """Whether the optional halves match the frequency that needs them.

        The application's copy of two CHECK constraints, and written as the
        same equalities they are so the two read alike. A weekly rule with no
        weekdays is a schedule that never fires; a daily one carrying weekdays
        is a row two readers would disagree about.
        """
        return (self.frequency is RecurrenceFrequency.WEEKLY) == bool(
            self.weekdays
        ) and (self.frequency is RecurrenceFrequency.MONTHLY) == (
            self.day_of_month is not None
        )


@dataclass(frozen=True, slots=True)
class RecurrenceEntity:
    """One stored schedule, read back.

    FLAT, with one field per column, and `rule` below is a property rather than
    a nested dataclass. That is not a style preference: `tests/test_integration
    _gates.py` walks every repository mapper and asserts it reads one row key
    per field of the entity it returns, which is the gate that caught
    `IssueEntity` gaining three fields on one branch while a mapper on another
    still built it from seven. A nested `rule: RecurrenceRule` would be a field
    with no column behind it, so the mapper could not satisfy that gate and the
    six values inside it would be exempt from the check that matters most here
    -- these columns arrive from a table a background sweep reads with nobody
    watching.

    `next_run_on` is the scheduler's own state -- see the migration -- and is
    carried so a settings screen can say when the next issue arrives without
    recomputing the calendar and risking a different answer from the sweep's.
    """

    template_id: UUID
    team_id: UUID

    frequency: RecurrenceFrequency
    interval_count: int
    weekdays: tuple[int, ...]
    day_of_month: int | None
    starts_on: date
    due_in_days: int | None

    next_run_on: date

    @property
    def rule(self) -> RecurrenceRule:
        """The schedule half of this row, as the arithmetic wants it.

        A property and not a stored value, so there is exactly one copy of
        every field: a `RecurrenceRule` built here from this row's own columns
        cannot disagree with them, where a second stored object could. The same
        judgement `IssueEntity.identifier` makes about a value derived from two
        columns it already holds.
        """
        return RecurrenceRule(
            frequency=self.frequency,
            interval_count=self.interval_count,
            starts_on=self.starts_on,
            weekdays=self.weekdays,
            day_of_month=self.day_of_month,
            due_in_days=self.due_in_days,
        )


def days_in_month(year: int, month: int) -> int:
    """How many days that month has, leap years included.

    `calendar.monthrange` and not a table of twelve numbers with a February
    branch beside it. The stdlib already knows about 1900 and 2000 and every
    other case somebody writes the branch wrong for.
    """
    return monthrange(year, month)[1]


def effective_day_of_month(rule_day: int, year: int, month: int) -> int:
    """The day of THAT month a "the Nth" rule actually lands on.

    THE CLAMP, and the reason a monthly recurrence on the 31st happens in
    February. `min(31, 28)` is the 28th, so a monthly obligation occurs every
    month; the alternative -- skip the month -- means a task that does not
    happen in February, which is never what anybody meant by "monthly", and
    loses an occurrence a year where clamping loses none.

    The clamp is applied to the RULE's day and never to the previous
    occurrence's, which is what stops it drifting. Computing "one month after
    the last run" gives Jan 31, Feb 28, Mar 28 -- and by summer the recurrence
    has silently walked back to the 28th forever. Anchoring on `rule_day` means
    February clamps and March returns to the 31st.
    """
    return min(rule_day, days_in_month(year, month))


def _monday_of(day: date) -> date:
    """The Monday that begins this day's ISO week.

    The unit weekly strides are counted in. Counting raw days between two
    arbitrary dates and dividing by seven answers a different question -- it
    depends on which weekday each of them fell on -- so both ends are pulled to
    their week's Monday first, and the difference is then whole weeks by
    construction.
    """
    return day - timedelta(days=day.isoweekday() - 1)


def _months_between(earlier: date, later: date) -> int:
    """Whole calendar months from one to the other, ignoring the day.

    The unit monthly strides are counted in, and it deliberately does not look
    at either day-of-month: "every three months on the 15th" strides over
    MONTHS, so January to April is three whatever days the two dates carry. A
    day-sensitive difference would make the stride depend on the clamp, which
    is the coupling `effective_day_of_month` exists to keep out.
    """
    return (later.year - earlier.year) * 12 + (later.month - earlier.month)


def occurs_on(rule: RecurrenceRule, day: date) -> bool:
    """Whether this rule names this calendar day.

    THE WHOLE SCHEDULE, as one predicate. Every question about a recurrence --
    when is the next one, was yesterday one, does the interval land here -- is
    this function asked about a day, which is what keeps the three frequencies
    from growing three separate pieces of arithmetic that have to agree.

    Nothing before `starts_on` is ever an occurrence. That is the bound half of
    the anchor, and it is checked first because the stride arithmetic below is
    only meaningful forwards: a negative month difference modulo an interval
    answers True for dates the schedule has nothing to say about.
    """
    if day < rule.starts_on:
        return False

    if rule.frequency is RecurrenceFrequency.DAILY:
        return (day - rule.starts_on).days % rule.interval_count == 0

    if rule.frequency is RecurrenceFrequency.WEEKLY:
        if day.isoweekday() not in rule.weekdays:
            return False

        weeks = (_monday_of(day) - _monday_of(rule.starts_on)).days // 7

        return weeks % rule.interval_count == 0

    # Monthly. `day_of_month` is guaranteed non-None by
    # `issue_recurrences_day_of_month_matches_frequency` and by
    # `RecurrenceRule.is_consistent`; the assertion is here so that a rule
    # constructed past both fails at the one place that reads the column rather
    # than comparing against None and quietly answering False forever.
    assert rule.day_of_month is not None, "a monthly rule carries a day of month"

    if day.day != effective_day_of_month(rule.day_of_month, day.year, day.month):
        return False

    return _months_between(rule.starts_on, day) % rule.interval_count == 0


def next_occurrence(rule: RecurrenceRule, *, after: date) -> date:
    """The first day this rule names STRICTLY after `after`.

    Strictly, which is what makes it usable as the advance: the sweep files
    today's issue and then asks for the next date after today, so a daily
    recurrence cannot claim itself twice in one day however many times the loop
    runs. A non-strict version would return today forever.

    `after` is the database's `CURRENT_DATE` on the claiming pass and never the
    process clock, so two replicas computing an advance in the same second
    compute it from the same day -- see `RecurrenceRepository.lock_due`, which
    returns that value alongside the rows.

    THE CATCH-UP RULE lives in the caller passing today rather than in here: a
    sweep that was down for three weeks advances a weekly recurrence to the next
    occurrence after TODAY and files ONE issue, instead of backfilling three
    identical ones nobody asked for. An obligation for a week that has gone is
    not something an issue filed now can discharge, and three copies of "weekly
    report" is the shape a person comes back from holiday to and archives in
    bulk. The cost is that the anchor shifts for the intervals above 1 -- a
    fortnightly recurrence resumes on the fortnight counted from its start, so
    it may land a week from where it would have -- which is visible on the
    settings screen and correctable by editing the schedule.
    """
    for offset in range(1, MAX_SCAN_DAYS + 1):
        candidate = after + timedelta(days=offset)

        if occurs_on(rule, candidate):
            return candidate

    raise UnreachableRecurrenceError()


def first_occurrence(rule: RecurrenceRule) -> date:
    """The day this rule fires for the first time, on or after `starts_on`.

    A separate function rather than `next_occurrence(after=starts_on -
    timedelta(days=1))` spelled at three call sites, because the off-by-one is
    the point: `starts_on` itself is an occurrence for a rule that names it --
    "every Monday from Monday" starts on that Monday -- and the strictly-after
    contract above would skip it.
    """
    if occurs_on(rule, rule.starts_on):
        return rule.starts_on

    return next_occurrence(rule, after=rule.starts_on)


def due_date_for(rule: RecurrenceRule, *, filed_on: date) -> date | None:
    """The generated issue's due date, or None for a recurrence with no due
    date.

    Counted from the day the issue is FILED rather than from the schedule's own
    `next_run_on`, and the two differ only when the sweep is catching up. Using
    the stored date would file an issue that is already overdue on the day it
    appears, which is a reminder waiting to fire about a promise nobody was
    there to make.
    """
    if rule.due_in_days is None:
        return None

    return filed_on + timedelta(days=rule.due_in_days)
