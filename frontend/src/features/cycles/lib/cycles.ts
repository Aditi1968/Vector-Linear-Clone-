/**
 * What a cycle is called, when it is, and which of the three phases it is in.
 *
 * The phase derivation is the only non-obvious thing in this feature, so it
 * is here on its own and covered by ./cycles.test.ts at the boundaries --
 * which is where a half-open interval is either right or silently wrong.
 */

import type { CycleFields } from '../api'

/**
 * A cycle relative to now.
 *
 * The backend does not label cycles. It has no `current` field, no `phase`
 * enum and no "active cycle" query; it returns `startsAt` and `endsAt` and
 * leaves the comparison to the caller. So this is a derivation, and it has to
 * agree exactly with the derivation the *database* makes, because the two are
 * describing the same instant.
 */
export type CyclePhase = 'current' | 'upcoming' | 'past'

/**
 * Which phase a cycle is in at `now`.
 *
 * ## The interval is half-open, and that is not a guess
 *
 * `migrations/008_cycles.sql` stores a cycle's span as
 * `tstzrange(starts_at, ends_at, '[)')` -- start inclusive, end exclusive --
 * and enforces `cycles_no_overlap` over it, so two of a team's cycles may
 * meet exactly at an instant: one's `endsAt` is the next one's `startsAt`.
 * That migration also writes out the query the range makes correct:
 *
 *     WHERE now() >= starts_at AND now() < ends_at
 *
 * This function is that query. Using `<=` on the end instead would make both
 * neighbours "current" for the one instant they share, and every screen would
 * then invent its own tiebreak -- which is precisely what the `[)` bound was
 * chosen to prevent.
 *
 * ## `now` is a parameter
 *
 * So that every cycle in one render is measured against the same instant --
 * a list computed across a tick boundary could otherwise show two current
 * cycles or none -- and so that the boundaries are testable without freezing
 * the clock.
 *
 * A cycle whose dates will not parse is `past`. That is a deliberate choice
 * over throwing: `NaN` comparisons are all false, so every naive branch would
 * call it "upcoming" and float it to the top of the screen. The database
 * refuses to store such a row (`starts_at`/`ends_at` are `TIMESTAMPTZ NOT
 * NULL`), so reaching this means something upstream is broken, and the
 * quietest place for a broken row is the bottom of the archive.
 */
export function cyclePhase(
  cycle: Pick<CycleFields, 'startsAt' | 'endsAt'>,
  now: number,
): CyclePhase {
  const startsAt = Date.parse(cycle.startsAt)
  const endsAt = Date.parse(cycle.endsAt)

  if (Number.isNaN(startsAt) || Number.isNaN(endsAt)) {
    return 'past'
  }

  if (now < startsAt) {
    return 'upcoming'
  }

  return now < endsAt ? 'current' : 'past'
}

export interface CyclesByPhase {
  /**
   * Cycles containing `now`.
   *
   * A list rather than a single cycle although `cycles_no_overlap` makes more
   * than one impossible *for one team*. This groups whatever it is given, and
   * the screen shows one team at a time -- but a function that returned
   * `Cycle | null` would be asserting the constraint rather than reading it,
   * and would silently discard a row if the constraint were ever relaxed.
   */
  current: CycleFields[]
  upcoming: CycleFields[]
  past: CycleFields[]
}

/**
 * Split a team's cycles into the three phases, each in the order it wants to
 * be read in.
 *
 * Upcoming ascends -- the next one first, because that is the one being
 * planned. Past descends -- the one just finished first, because that is the
 * one being reviewed. The server returns them in `number` order
 * (`app/repositories/cycles.py`), which is neither, so both are sorted here
 * rather than left to a reader's assumption.
 *
 * Sorted on the parsed timestamp and not on the string: ISO-8601 strings sort
 * correctly only while they share an offset, and these carry whatever offset
 * the server sent.
 */
export function groupByPhase(
  cycles: readonly CycleFields[],
  now: number,
): CyclesByPhase {
  const grouped: CyclesByPhase = { current: [], upcoming: [], past: [] }

  for (const cycle of cycles) {
    grouped[cyclePhase(cycle, now)].push(cycle)
  }

  const startsAt = (cycle: CycleFields) => Date.parse(cycle.startsAt)

  grouped.current.sort((a, b) => startsAt(a) - startsAt(b))
  grouped.upcoming.sort((a, b) => startsAt(a) - startsAt(b))
  grouped.past.sort((a, b) => startsAt(b) - startsAt(a))

  return grouped
}

/**
 * What to call a cycle.
 *
 * `name` is nullable and most cycles have none -- a team runs "Cycle 12", not
 * "Q3 hardening". The number is always there and is unique within the team,
 * so it is the fallback rather than a blank or an id.
 */
export function cycleTitle(cycle: Pick<CycleFields, 'number' | 'name'>): string {
  return cycle.name ?? `Cycle ${String(cycle.number)}`
}

/**
 * A cycle's dates, formatted in the viewer's locale.
 *
 * `startsAt`/`endsAt` are the `DateTime` scalar -- real instants with an
 * offset, not calendar days -- so unlike a project's target date these are
 * formatted in *local* time, which is the timezone the boundary actually
 * falls at for the person reading. Only the date is shown: a cycle is a
 * fortnight, and the minute it turns over is noise.
 */
const DATE_FORMAT = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium' })

export function formatCycleDate(value: string): string {
  const parsed = new Date(value)

  return Number.isNaN(parsed.getTime()) ? value : DATE_FORMAT.format(parsed)
}

/** "3 Mar – 17 Mar", as one string for a line of meta. */
export function formatCycleRange(cycle: Pick<CycleFields, 'startsAt' | 'endsAt'>): string {
  return `${formatCycleDate(cycle.startsAt)} – ${formatCycleDate(cycle.endsAt)}`
}

/**
 * The value an `<input type="datetime-local">` shows for an instant.
 *
 * The native control has no timezone: it displays and returns a *local* wall
 * time as `YYYY-MM-DDTHH:mm`. So an instant has to be shifted into local
 * terms before it can be shown, and shifted back before it is sent -- which
 * `new Date(localString)` does, since a datetime-local string with no offset
 * is parsed as local time. Doing this with `toISOString().slice(0, 16)` is
 * the usual bug: that yields UTC wall time, and the form then shows a cycle
 * starting at 09:00 as starting at 01:00 in California.
 */
export function toDateTimeLocal(value: string): string {
  const parsed = new Date(value)

  if (Number.isNaN(parsed.getTime())) {
    return ''
  }

  const offsetMinutes = parsed.getTimezoneOffset()

  return new Date(parsed.getTime() - offsetMinutes * 60_000).toISOString().slice(0, 16)
}

/** The inverse: a `datetime-local` value as the ISO instant the API takes. */
export function fromDateTimeLocal(value: string): string | null {
  const parsed = new Date(value)

  return Number.isNaN(parsed.getTime()) ? null : parsed.toISOString()
}
