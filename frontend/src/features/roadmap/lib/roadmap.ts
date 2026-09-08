/**
 * Turning two lists into a calendar.
 *
 * ## What the schema can support, and what it cannot
 *
 * `Initiative.targetDate` and `Project.targetDate` are the only scheduling
 * fields either type has. Migrations 009 and 022 store one `target_date DATE`
 * per row: no start date, no duration, no percent complete, and nothing that
 * says when work began.
 *
 * So every item here is a POINT ON A DATE and never a bar between two. That
 * is the whole reason this module groups rather than measures. A Gantt bar
 * would need a start, the only candidate is `createdAt` -- the moment the row
 * was inserted -- and a bar drawn from there would be a picture of a number
 * nobody chose. This module does not produce one, and there is no place in
 * its output to put one.
 *
 * Everything below is a pure function of what the server sent. Nothing
 * invents a date, infers one from a neighbour, or moves an item into a month
 * it did not name.
 */

import type { BadgeTone } from '../../../components'
import { initiativeStatusLabel, initiativeStatusTone } from '../../initiatives/lib/initiatives'
import { projectStateLabel, projectStateTone } from '../../projects/lib/projects'
import type { Health, RoadmapInitiative, RoadmapProject } from '../api'

/** Which of the two lists an item came from. The screen labels them. */
export type RoadmapItemKind = 'initiative' | 'project'

/**
 * One thing on the calendar, from either list.
 *
 * A flattened shape rather than a discriminated union of the two fragments,
 * because after the state has been named and toned the two are the same thing
 * to a calendar and every consumer would otherwise re-branch on `kind`.
 * `kind` survives so the screen can say which is which and link correctly.
 */
export interface RoadmapItem {
  kind: RoadmapItemKind
  id: string
  name: string
  /** `YYYY-MM-DD`, or null when nothing has been scheduled. */
  targetDate: string | null
  health: Health | null
  stateLabel: string
  stateTone: BadgeTone
  /**
   * The work is finished or abandoned.
   *
   * What it is *for* is the overdue test: a project that shipped last March
   * is not late, it is done, and a red flag on it would be noise that trains
   * people to ignore the flag.
   */
  isClosed: boolean
}

export function toRoadmapItems(
  initiatives: readonly RoadmapInitiative[],
  projects: readonly RoadmapProject[],
): RoadmapItem[] {
  const fromInitiatives = initiatives.map((initiative): RoadmapItem => ({
    kind: 'initiative',
    id: initiative.id,
    name: initiative.name,
    targetDate: initiative.targetDate,
    health: initiative.health,
    stateLabel: initiativeStatusLabel(initiative.status),
    stateTone: initiativeStatusTone(initiative.status),
    isClosed: initiative.status === 'COMPLETED' || initiative.status === 'CANCELED',
  }))

  const fromProjects = projects.map((project): RoadmapItem => ({
    kind: 'project',
    id: project.id,
    name: project.name,
    targetDate: project.targetDate,
    health: project.health,
    stateLabel: projectStateLabel(project.state),
    stateTone: projectStateTone(project.state),
    isClosed: project.state === 'COMPLETED' || project.state === 'CANCELED',
  }))

  return [...fromInitiatives, ...fromProjects]
}

/** One column of the calendar. */
export interface RoadmapMonth {
  /** `YYYY-MM`. Sorts lexicographically into date order, which is why. */
  key: string
  /** The month as the viewer's locale writes it. */
  label: string
  items: RoadmapItem[]
}

export interface RoadmapLayout {
  months: RoadmapMonth[]
  /**
   * Items with no target date.
   *
   * Shown, always, and never folded into a month. An unscheduled project is
   * the single most useful thing a roadmap can point at, and it is exactly
   * what a calendar-shaped view drops if nobody decides otherwise.
   */
  undated: RoadmapItem[]
}

/**
 * `YYYY-MM` from the `Date` scalar.
 *
 * The scalar is produced by PostgreSQL from a `DATE` column, so it is always
 * `YYYY-MM-DD` -- but this is still a parse and not a slice. A value that is
 * not that shape is not forced into a month it might not belong to; it is
 * reported as unschedulable, and the caller shows it beside the genuinely
 * undated rather than silently mis-filing it into a column.
 */
const CALENDAR_DAY = /^(\d{4}-\d{2})-\d{2}$/

function monthKey(targetDate: string): string | null {
  return CALENDAR_DAY.exec(targetDate)?.[1] ?? null
}

/**
 * The month label, in the viewer's locale.
 *
 * `timeZone: 'UTC'` for the reason `formatDay` in
 * `features/projects/lib/projects.ts` gives: `new Date('2026-03-01')` parses
 * as UTC midnight, and formatting that in local time west of Greenwich yields
 * February -- a project due in March filed under the wrong month for everyone
 * in the Americas.
 */
const MONTH_FORMAT = new Intl.DateTimeFormat(undefined, {
  month: 'long',
  year: 'numeric',
  timeZone: 'UTC',
})

function monthLabel(key: string): string {
  const parsed = new Date(`${key}-01T00:00:00Z`)

  return Number.isNaN(parsed.getTime()) ? key : MONTH_FORMAT.format(parsed)
}

/**
 * Lay the items out as months, in date order.
 *
 * Only months that hold something get a column. Filling the gaps would draw a
 * continuous axis, which is what a roadmap usually looks like -- and would
 * also mean inventing however many empty columns lie between two distant
 * dates, which for a workspace holding one thing due next week and one due in
 * 2030 is seventy columns of nothing. The dates themselves are on every item,
 * so a gap is readable from the headings.
 *
 * Within a month, earlier dates first, then by name so the order is total and
 * a re-render cannot reshuffle equal rows.
 */
export function groupByMonth(items: readonly RoadmapItem[]): RoadmapLayout {
  const byMonth = new Map<string, RoadmapItem[]>()
  const undated: RoadmapItem[] = []

  for (const item of items) {
    const key = item.targetDate === null ? null : monthKey(item.targetDate)

    if (key === null) {
      undated.push(item)
      continue
    }

    const column = byMonth.get(key)

    if (column === undefined) {
      byMonth.set(key, [item])
    } else {
      column.push(item)
    }
  }

  const months = [...byMonth.entries()]
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([key, monthItems]) => ({
      key,
      label: monthLabel(key),
      items: monthItems.sort(
        (left, right) =>
          (left.targetDate ?? '').localeCompare(right.targetDate ?? '') ||
          left.name.localeCompare(right.name),
      ),
    }))

  undated.sort((left, right) => left.name.localeCompare(right.name))

  return { months, undated }
}

/**
 * Past its target date and not finished.
 *
 * `now` is a parameter with a default rather than a call to `Date.now()`
 * inside, so a test can pin the day instead of writing dates relative to
 * whenever the suite runs.
 *
 * The comparison is on the date string, not on parsed instants: both sides
 * are `YYYY-MM-DD` in UTC, lexicographic order on that is chronological
 * order, and it cannot be knocked a day either way by the viewer's timezone.
 */
export function isOverdue(item: RoadmapItem, now: number = Date.now()): boolean {
  if (item.targetDate === null || item.isClosed) {
    return false
  }

  const today = new Date(now).toISOString().slice(0, 10)

  return item.targetDate < today
}
