"""Recurrence arithmetic, which is where this feature is silently wrong or not.

Everything else in the recurring-issue path fails loudly: a bad template id is
"not found", a bad frequency is a CHECK violation, a template that cannot be
applied is an exception in a log. The calendar is the part that produces a
plausible date on the wrong day and then does it every month for a year, so
this file is longer than the module it tests.

Four claims it exists to hold, in the order they go wrong in the wild:

  * THE 31st DOES NOT DRIFT. Clamping February to the 28th is the easy half;
    the half that gets written wrong is computing the NEXT date from the
    clamped one, which walks a monthly recurrence back to the 28th forever.
  * THE INTERVAL IS COUNTED FROM THE ANCHOR. "Every two weeks on Tuesday" is
    meaningless without a week to count from, and an implementation that
    counted from the previous instance silently shifts by a week after any
    delay.
  * A GAP IS NOT BACKFILLED. Three weeks of downtime files one issue, not
    three.
  * NOTHING HERE HAS A CLOCK. Every value is a `datetime.date`, which is the
    whole DST answer -- a transition moves instants and this module holds none.

No `db` mark: nothing here reaches PostgreSQL. tests/test_migration_029_db.py
is where the constraints these values satisfy are exercised.
"""

from datetime import date, timedelta

import pytest

from app.domain.recurrence import (
    MAX_SCAN_DAYS,
    RecurrenceFrequency,
    RecurrenceRule,
    UnreachableRecurrenceError,
    due_date_for,
    effective_day_of_month,
    first_occurrence,
    next_occurrence,
    occurs_on,
)


# A Monday, chosen so that every weekday assertion below reads as the day it
# names rather than as an offset somebody has to count.
MONDAY = date(2026, 1, 5)


def daily(interval: int = 1, starts_on: date = MONDAY, **kwargs) -> RecurrenceRule:
    return RecurrenceRule(
        frequency=RecurrenceFrequency.DAILY,
        interval_count=interval,
        starts_on=starts_on,
        **kwargs,
    )


def weekly(
    *weekdays: int,
    interval: int = 1,
    starts_on: date = MONDAY,
    **kwargs,
) -> RecurrenceRule:
    return RecurrenceRule(
        frequency=RecurrenceFrequency.WEEKLY,
        interval_count=interval,
        starts_on=starts_on,
        weekdays=tuple(weekdays),
        **kwargs,
    )


def monthly(
    day: int,
    *,
    interval: int = 1,
    starts_on: date = MONDAY,
    **kwargs,
) -> RecurrenceRule:
    return RecurrenceRule(
        frequency=RecurrenceFrequency.MONTHLY,
        interval_count=interval,
        starts_on=starts_on,
        day_of_month=day,
        **kwargs,
    )


def walk(rule: RecurrenceRule, count: int) -> list[date]:
    """The first `count` occurrences, as the sweep would produce them.

    Each one is `next_occurrence` from the last, which is exactly what
    `ScheduleWorker._generate` does with `advance` -- so a drift that only
    appears after several steps appears here too. A single-step assertion
    would pass for the clamped-from-the-previous-instance bug that this file
    exists to catch.
    """
    dates = [first_occurrence(rule)]

    while len(dates) < count:
        dates.append(next_occurrence(rule, after=dates[-1]))

    return dates


# --- daily -----------------------------------------------------------


def test_a_daily_rule_names_every_day_from_its_start():
    assert walk(daily(), 4) == [
        MONDAY,
        MONDAY + timedelta(days=1),
        MONDAY + timedelta(days=2),
        MONDAY + timedelta(days=3),
    ]


def test_an_interval_skips_the_days_between():
    assert walk(daily(interval=3), 3) == [
        MONDAY,
        MONDAY + timedelta(days=3),
        MONDAY + timedelta(days=6),
    ]


def test_nothing_before_the_anchor_is_an_occurrence():
    """The bound half of `starts_on`, and it has to be checked FIRST.

    Modulo arithmetic over a negative difference answers True for dates the
    schedule has nothing to say about, so an implementation that strided before
    it bounded would report that a recurrence starting in April occurs in
    January.
    """
    rule = daily(interval=3)

    assert not occurs_on(rule, MONDAY - timedelta(days=3))
    assert not occurs_on(rule, MONDAY - timedelta(days=1))
    assert occurs_on(rule, MONDAY)


# --- weekly ----------------------------------------------------------


def test_a_weekly_rule_names_only_the_weekdays_it_carries():
    """Monday and Thursday, twice over, in the order a calendar has them."""
    assert walk(weekly(1, 4), 4) == [
        MONDAY,
        MONDAY + timedelta(days=3),
        MONDAY + timedelta(days=7),
        MONDAY + timedelta(days=10),
    ]


def test_a_weekday_before_the_anchor_in_its_own_week_is_not_an_occurrence():
    """Anchored on a Wednesday, the Monday of that same week is in the past.

    The interval arithmetic says that week qualifies -- it is week zero -- so
    only the `starts_on` bound keeps this from firing two days before the
    schedule was saved. The failure would be invisible: an issue filed for a
    day that had already gone.
    """
    wednesday = MONDAY + timedelta(days=2)
    rule = weekly(1, 3, starts_on=wednesday)

    assert not occurs_on(rule, MONDAY)
    assert occurs_on(rule, wednesday)


def test_a_fortnightly_rule_counts_weeks_from_the_anchor_and_not_from_the_last_run():
    """Every two weeks on Tuesday and Friday, anchored on a Monday.

    The two days inside one week are both occurrences, and the following week
    has neither -- which is the property that fails if the stride is counted
    from the previous occurrence: Friday to the next Tuesday is four days, so
    "two weeks since the last one" would put the next Tuesday eleven days out
    and the pattern would walk.
    """
    assert walk(weekly(2, 5, interval=2), 5) == [
        MONDAY + timedelta(days=1),  # Tue, week 0
        MONDAY + timedelta(days=4),  # Fri, week 0
        MONDAY + timedelta(days=15),  # Tue, week 2
        MONDAY + timedelta(days=18),  # Fri, week 2
        MONDAY + timedelta(days=29),  # Tue, week 4
    ]


def test_the_week_a_stride_counts_is_the_iso_week_and_not_seven_raw_days():
    """Anchored mid-week, so raw-day arithmetic and week arithmetic disagree.

    Anchored on a Friday with a fortnightly Monday: the Monday three days later
    is in the NEXT ISO week, which is week 1 and therefore skipped, and the
    first occurrence is the Monday ten days out in week 2. An implementation
    dividing raw days by seven would answer week 0 for that first Monday and
    fire a week early, every other week, forever.
    """
    friday = MONDAY + timedelta(days=4)

    assert first_occurrence(weekly(1, interval=2, starts_on=friday)) == (
        friday + timedelta(days=10)
    )


# --- monthly, and the 31st -------------------------------------------


@pytest.mark.parametrize(
    ("rule_day", "year", "month", "expected"),
    [
        (31, 2026, 1, 31),
        # February in a common year and in a leap year. Both come from
        # `calendar.monthrange` rather than from a table with a leap branch
        # somebody wrote.
        (31, 2026, 2, 28),
        (31, 2028, 2, 29),
        (31, 2026, 4, 30),
        (15, 2026, 2, 15),
        (29, 2026, 2, 28),
    ],
)
def test_a_day_past_the_end_of_the_month_clamps_to_its_last_day(
    rule_day, year, month, expected
):
    assert effective_day_of_month(rule_day, year, month) == expected


def test_the_31st_clamps_in_february_and_returns_to_the_31st_in_march():
    """THE ASSERTION THIS FILE EXISTS FOR.

    The clamp is applied to the RULE's day every month, never to the previous
    occurrence's. Computing "one month after the last run" gives Jan 31, Feb 28,
    Mar 28 -- and a monthly recurrence has silently moved to the 28th for the
    rest of its life, which nobody notices because every date it produces looks
    reasonable.

    Four months, because three would pass for an implementation that clamped
    once and then drifted: the return to the 31st in March is the step that
    distinguishes them.
    """
    assert walk(monthly(31, starts_on=date(2026, 1, 31)), 4) == [
        date(2026, 1, 31),
        date(2026, 2, 28),
        date(2026, 3, 31),
        date(2026, 4, 30),
    ]


def test_a_monthly_interval_strides_over_months_and_not_over_days():
    """Every three months on the 15th, which is not "every 90 days".

    Months differ in length, so a day-based stride drifts through the calendar
    -- and the drift is slow enough to look like a rounding artefact for the
    first year.
    """
    assert walk(monthly(15, interval=3, starts_on=date(2026, 1, 15)), 4) == [
        date(2026, 1, 15),
        date(2026, 4, 15),
        date(2026, 7, 15),
        date(2026, 10, 15),
    ]


def test_a_monthly_stride_crosses_the_year_boundary():
    """`_months_between` has to count across years, which a naive
    `later.month - earlier.month` does not: December to February would be -10.
    """
    assert walk(monthly(1, interval=2, starts_on=date(2026, 11, 1)), 3) == [
        date(2026, 11, 1),
        date(2027, 1, 1),
        date(2027, 3, 1),
    ]


def test_a_clamped_month_does_not_change_which_months_qualify():
    """Every two months on the 31st, starting in January.

    March qualifies (two months on) and February does not, so the clamp in
    March is 31 and the schedule never sees February at all. The test is here
    because the interval and the clamp are the two pieces most likely to be
    written as one expression that answers a different question.
    """
    assert walk(monthly(31, interval=2, starts_on=date(2026, 1, 31)), 3) == [
        date(2026, 1, 31),
        date(2026, 3, 31),
        date(2026, 5, 31),
    ]


# --- the first occurrence and the strictly-after contract ------------


def test_the_start_day_is_itself_an_occurrence_when_the_rule_names_it():
    """`first_occurrence` is not `next_occurrence(after=starts_on)`.

    A schedule saved for "every Monday, from this Monday" fires today. The
    strictly-after contract that makes the advance safe would skip it, which is
    why the two are separate functions rather than one with an off-by-one at
    three call sites.
    """
    assert first_occurrence(weekly(1)) == MONDAY
    assert next_occurrence(weekly(1), after=MONDAY) == MONDAY + timedelta(days=7)


def test_the_start_day_is_skipped_when_the_rule_does_not_name_it():
    """Anchored on a Monday with a Wednesday-only schedule."""
    assert first_occurrence(weekly(3)) == MONDAY + timedelta(days=2)


def test_the_advance_is_strictly_after_so_a_daily_rule_cannot_claim_twice():
    """The property that stops a sweep filing two issues in one day.

    `ScheduleWorker` files today's issue and then advances with
    `after=today`; a non-strict `next_occurrence` would answer today again, the
    row would still be due, and the next pass fifteen minutes later would file
    another one.
    """
    rule = daily()

    assert next_occurrence(rule, after=MONDAY) > MONDAY


# --- catching up after an outage -------------------------------------


def test_a_gap_advances_past_today_rather_than_backfilling_it():
    """Three weeks of downtime files ONE issue and moves on.

    The catch-up rule lives in the caller passing today rather than the stored
    date, and this is the assertion that pins it: the next run is the first
    occurrence after TODAY, not the one after the date the schedule was stuck
    on. Backfilling would produce three identical "weekly report" issues, which
    is what somebody returning from holiday archives in bulk.
    """
    rule = weekly(1)
    stuck_on = MONDAY
    today = MONDAY + timedelta(days=21)

    assert next_occurrence(rule, after=today) == today + timedelta(days=7)
    assert next_occurrence(rule, after=stuck_on) == MONDAY + timedelta(days=7)


# --- the generated issue's due date ----------------------------------


def test_the_due_date_is_counted_from_the_day_the_issue_is_filed():
    """Not from the schedule's stored date, which after an outage is past.

    An issue filed today and dated three weeks ago is born overdue, which is a
    reminder waiting to fire about a promise nobody was there to make.
    """
    rule = weekly(1, due_in_days=3)
    filed_on = MONDAY + timedelta(days=21)

    assert due_date_for(rule, filed_on=filed_on) == filed_on + timedelta(days=3)


def test_a_schedule_with_no_offset_files_an_issue_with_no_due_date():
    """An ordinary state: a daily checklist is due when it is due."""
    assert due_date_for(weekly(1), filed_on=MONDAY) is None


def test_a_zero_offset_is_due_the_day_it_is_filed():
    """Zero is a real value and not a missing one, so it must not read as None
    -- which is what a truthiness check here would do."""
    assert due_date_for(weekly(1, due_in_days=0), filed_on=MONDAY) == MONDAY


# --- consistency, and the bound --------------------------------------


@pytest.mark.parametrize(
    ("rule", "consistent"),
    [
        (weekly(1), True),
        (monthly(15), True),
        (daily(), True),
        # A weekly rule with no weekdays never fires; a daily one carrying them
        # is a row two readers would disagree about. Both are refused by
        # `issue_recurrences_weekdays_match_frequency`, and this is the
        # application's copy so a client is told which field is wrong.
        (
            RecurrenceRule(
                frequency=RecurrenceFrequency.WEEKLY,
                interval_count=1,
                starts_on=MONDAY,
            ),
            False,
        ),
        (
            RecurrenceRule(
                frequency=RecurrenceFrequency.DAILY,
                interval_count=1,
                starts_on=MONDAY,
                weekdays=(1,),
            ),
            False,
        ),
        (
            RecurrenceRule(
                frequency=RecurrenceFrequency.MONTHLY,
                interval_count=1,
                starts_on=MONDAY,
            ),
            False,
        ),
        (
            RecurrenceRule(
                frequency=RecurrenceFrequency.WEEKLY,
                interval_count=1,
                starts_on=MONDAY,
                weekdays=(1,),
                day_of_month=5,
            ),
            False,
        ),
    ],
)
def test_the_optional_halves_must_match_the_frequency_that_needs_them(rule, consistent):
    assert rule.is_consistent is consistent


def test_the_scan_is_bounded_and_says_so_rather_than_returning_a_wrong_day():
    """A rule the CHECK constraints could not produce, refused loudly.

    The forward scan is what keeps the arithmetic reviewable, and its bound is
    what keeps a malformed rule from being an infinite loop inside a background
    task. Reached here with a weekday set the constraints forbid -- 0 is not an
    ISO weekday -- because every rule they DO admit has an occurrence inside
    `MAX_SCAN_DAYS`, which is the point.
    """
    impossible = weekly(0)

    with pytest.raises(UnreachableRecurrenceError):
        next_occurrence(impossible, after=MONDAY)


def test_the_bound_covers_the_widest_rule_the_constraints_admit():
    """Fifty-two months on the 31st, which is the furthest legitimate gap.

    Asserted rather than assumed, because `MAX_SCAN_DAYS` is a number somebody
    could tighten while looking at the weekly case and never notice it had
    stopped covering the monthly one -- and the symptom would be an exception
    from a background task nobody is watching.
    """
    widest = monthly(31, interval=52, starts_on=date(2026, 1, 31))

    assert next_occurrence(widest, after=date(2026, 1, 31)) == date(2030, 5, 31)
    assert MAX_SCAN_DAYS >= 52 * 31


def test_nothing_in_this_module_holds_a_time():
    """The DST answer, asserted rather than only argued in a docstring.

    A daylight-saving transition moves INSTANTS. Every value this module
    produces is a `date`, so there is no instant for one to move -- which is
    migration 006's "the day something is expected is a DATE" paying off. A
    `datetime` slipping in here would compare and subtract without complaint
    and would shift by an hour twice a year.
    """
    rule = monthly(31, due_in_days=2, starts_on=date(2026, 1, 31))
    produced = [
        first_occurrence(rule),
        next_occurrence(rule, after=date(2026, 3, 1)),
        due_date_for(rule, filed_on=MONDAY),
    ]

    for value in produced:
        assert type(value) is date, f"{value!r} is not a plain date"
