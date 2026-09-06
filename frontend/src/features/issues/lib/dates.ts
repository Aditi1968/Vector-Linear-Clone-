/**
 * Rendering the three timestamps the schema exposes.
 *
 * `createdAt`, `updatedAt` and `completedAt` arrive as ISO-8601 strings with
 * an offset. Nothing here invents a timestamp the server did not send: a
 * value that will not parse is returned unchanged rather than replaced with
 * "unknown" or with today's date.
 *
 * The formatters are built once at module scope. `Intl.DateTimeFormat` is
 * expensive to construct and cheap to reuse, and building one per row of a
 * 25-row list per render is a measurable cost for no benefit.
 *
 * Locale is left as the user's. Passing `undefined` is deliberate rather than
 * an omission -- hardcoding `en-US` would render a German user's dates in the
 * wrong order for the sake of a stable-looking screenshot.
 */

const ABSOLUTE_FORMAT = new Intl.DateTimeFormat(undefined, {
  dateStyle: 'medium',
  timeStyle: 'short',
})

const RELATIVE_FORMAT = new Intl.RelativeTimeFormat(undefined, {
  numeric: 'auto',
})

/** For the `Date` scalar: a calendar day, with no time and no timezone. */
const DAY_FORMAT = new Intl.DateTimeFormat(undefined, {
  month: 'short',
  day: 'numeric',
  year: 'numeric',
})

/** `YYYY-MM-DD`, which is the only shape the `Date` scalar is sent in. */
const CALENDAR_DAY = /^(\d{4})-(\d{2})-(\d{2})$/

/**
 * Largest-first, so the first threshold an elapsed span clears is the unit it
 * is described in. Approximate by design: "3 months ago" does not need to
 * know which months.
 */
const RELATIVE_DIVISIONS: readonly (readonly [Intl.RelativeTimeFormatUnit, number])[] = [
  ['year', 365 * 24 * 60 * 60 * 1000],
  ['month', 30 * 24 * 60 * 60 * 1000],
  ['week', 7 * 24 * 60 * 60 * 1000],
  ['day', 24 * 60 * 60 * 1000],
  ['hour', 60 * 60 * 1000],
  ['minute', 60 * 1000],
]

function parse(value: string): Date | null {
  const parsed = new Date(value)

  return Number.isNaN(parsed.getTime()) ? null : parsed
}

/** An exact, locale-formatted date and time. Used wherever precision matters. */
export function formatAbsolute(value: string): string {
  const parsed = parse(value)

  return parsed === null ? value : ABSOLUTE_FORMAT.format(parsed)
}

/**
 * A due date, which is a calendar day and not an instant.
 *
 * Parsed field by field rather than with `new Date(value)`, and that is the
 * whole reason this is not `formatAbsolute`. `new Date('2026-03-14')` is
 * specified to parse a date-only string as UTC midnight, so every viewer west
 * of Greenwich would see a due date one day early -- the classic off-by-one
 * that only shows up for some of your users, in some months of the year.
 * Constructing the date from its parts puts it at local midnight, where a day
 * the whole team agreed on belongs.
 *
 * A value that is not `YYYY-MM-DD` is returned unchanged, in keeping with the
 * rest of this module: nothing here invents a date the server did not send.
 */
export function formatDueDate(value: string): string {
  const parts = CALENDAR_DAY.exec(value)

  if (parts === null) {
    return value
  }

  const [, year, month, day] = parts
  const parsed = new Date(Number(year), Number(month) - 1, Number(day))

  return Number.isNaN(parsed.getTime()) ? value : DAY_FORMAT.format(parsed)
}

/**
 * "3 days ago", "in 2 hours", "now".
 *
 * `now` is a parameter rather than a call to `Date.now()` inside the loop so
 * that every row of one render is measured against the same instant, and so
 * that this is testable without freezing the clock.
 */
export function formatRelative(value: string, now: number = Date.now()): string {
  const parsed = parse(value)

  if (parsed === null) {
    return value
  }

  const elapsed = parsed.getTime() - now

  for (const [unit, span] of RELATIVE_DIVISIONS) {
    if (Math.abs(elapsed) >= span) {
      return RELATIVE_FORMAT.format(Math.round(elapsed / span), unit)
    }
  }

  return RELATIVE_FORMAT.format(Math.round(elapsed / 1000), 'second')
}
