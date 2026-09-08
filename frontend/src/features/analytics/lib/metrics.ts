/**
 * Turning the aggregate into things a person can read, and saying what is not
 * there.
 *
 * Every function here is pure and every one of them exists because the naive
 * rendering would be a lie of a specific kind. `features/semanticSearch/lib/
 * indexState.ts` is the model: an empty answer and an unbuilt index look
 * identical on a screen, and only one of them is a fact about the workspace.
 *
 * The three lies this module is written against:
 *
 *   1. **A chart of zeros looks like a chart of nothing.** A day with no
 *      completions and a day the server never reported render as the same
 *      flat line. The server zero-fills, so the series is always complete --
 *      and `describeWindow` says so, rather than leaving the reader to guess.
 *   2. **A median over three issues looks like a median.** Every duration
 *      arrives with the sample it was taken over, and `describeSample` puts
 *      that in words next to the figure.
 *   3. **A partial metric looks total.** Cycle time can only be measured for
 *      issues with a recorded start, and `describeCycleTimeCoverage` is what
 *      keeps the number from being read as though it covered all of them.
 */

import type {
  CycleTimeSummary,
  DurationSummary,
  EstimateScale,
  PriorityCount,
  StateCategoryCount,
  ThroughputDay,
  WorkflowStateCategory,
} from '../api'

const HOURS_PER_DAY = 24

/**
 * A duration in hours, as the shortest form that is still precise.
 *
 * Hours below a day, days above it, and one decimal place on each. A cycle
 * time of "37.2 hours" is harder to compare than "1.6 days", and "0.04 days"
 * is a number nobody can picture.
 *
 * Rounded and never truncated: 23.97 hours reading as "23.9 hours" is wrong in
 * the direction that flatters, and this whole screen is written against
 * numbers that flatter.
 */
export function formatHours(hours: number): string {
  if (!Number.isFinite(hours) || hours < 0) {
    return '—'
  }

  if (hours < HOURS_PER_DAY) {
    return `${(Math.round(hours * 10) / 10).toString()}h`
  }

  return `${(Math.round((hours / HOURS_PER_DAY) * 10) / 10).toString()}d`
}

/**
 * A completion rate as a percentage, or the reason there is not one.
 *
 * `null` is not zero and is not rendered as zero. The server sends null when
 * nothing stopped in the window, and "0%" for a fortnight in which nothing was
 * abandoned either reports a failure that did not happen.
 */
export function formatRate(rate: number | null): string {
  if (rate === null) {
    return '—'
  }

  return `${Math.round(rate * 100).toString()}%`
}

/** The sample a percentile was taken over, in words, or a warning about it. */
export function describeSample(summary: DurationSummary | null): string {
  if (summary === null) {
    return 'Nothing finished in this window, so there is no duration to report.'
  }

  if (summary.count < 5) {
    return `Over ${summary.count.toString()} ${summary.count === 1 ? 'issue' : 'issues'} — too few to read a trend into.`
  }

  return `Over ${summary.count.toString()} issues.`
}

/**
 * What cycle time actually covers, said out loud beside it.
 *
 * The metric is measured from the first recorded transition into a started
 * state, and that instant is not a column on the issue -- it is read from the
 * activity history, which begins at one migration and records nothing for an
 * issue dragged straight from backlog to done. So a median over "the issues
 * that happen to have a recorded start" is systematically better than the
 * median of the work, because the issues with a history are the ones that were
 * worked deliberately by people who move cards.
 *
 * Returning a sentence rather than a boolean because there are three
 * situations and only one of them permits showing the number at all.
 */
export interface CycleTimeCoverage {
  /** The median may be shown. False means it must not be. */
  isMeasurable: boolean
  /** Reported for every case, including the one that permits the figure. */
  note: string
}

export function describeCycleTimeCoverage(
  cycleTime: CycleTimeSummary | null,
): CycleTimeCoverage {
  if (cycleTime === null) {
    return {
      isMeasurable: false,
      note: 'Nothing was delivered in this window, so there is no time in progress to measure.',
    }
  }

  if (cycleTime.measured === 0) {
    return {
      isMeasurable: false,
      note: `None of the ${cycleTime.completedTotal.toString()} issues delivered in this window has a recorded start, so time in progress cannot be measured for any of them. Lead time beside it is measured from every one.`,
    }
  }

  if (cycleTime.measured < cycleTime.completedTotal) {
    return {
      isMeasurable: true,
      note: `Measured over ${cycleTime.measured.toString()} of the ${cycleTime.completedTotal.toString()} issues delivered — the rest have no recorded start, because Vector reads it from the activity history rather than from a column. Issues moved straight to done are not in this figure.`,
    }
  }

  return {
    isMeasurable: true,
    note: `Measured over all ${cycleTime.completedTotal.toString()} issues delivered in this window.`,
  }
}

/**
 * What the window covers, and the two things about it a reader cannot see.
 *
 * Days are UTC for every viewer -- nothing in the schema records a person's
 * timezone -- so somebody west of Greenwich sees their evening's work on the
 * next day's bar. That is a real and permanent skew and it is stated rather
 * than left to be discovered.
 */
export function describeWindow(rangeStart: string, rangeEnd: string, days: number): string {
  return `${days.toString()} days, ${rangeStart} to ${rangeEnd}. Days are UTC, so work finished late in the evening west of Greenwich counts on the following day.`
}

/**
 * The unit a team's estimates are in, as a word to print beside a number.
 *
 * `null` for the t-shirt scale, and that is the whole mixed-scale rule in one
 * return value: a t-shirt estimate is a rung on a ladder, so a sum of them is
 * not a quantity in any unit and there is no word for it. The server sends
 * null for the total; this sends null for the label, and the screen prints
 * neither.
 */
export function estimateUnit(scale: EstimateScale): string | null {
  switch (scale) {
    case 'POINTS':
      return 'points'
    case 'HOURS':
      return 'hours'
    case 'NONE':
      return 'units'
    default:
      // TSHIRT. Sizes are named rungs, and 2 + 3 of them is not 5 of anything.
      return null
  }
}

/**
 * An estimate total with its unit, or the reason there is not one.
 *
 * Three outcomes, kept apart because they call for opposite conclusions:
 * a real total, "the unit does not add up", and "nobody sized any of it".
 * The server distinguishes the last two by sending a null total alongside an
 * `estimated` count, and collapsing them would tell a team that estimates
 * diligently in t-shirt sizes that it estimates nothing.
 */
export function describeEstimateTotal(
  total: number | null,
  estimated: number,
  scale: EstimateScale,
): string {
  const unit = estimateUnit(scale)

  if (unit === null) {
    return estimated === 0
      ? 'Not sized'
      : `${estimated.toString()} sized — t-shirt sizes do not sum`
  }

  if (total === null) {
    return 'Not sized'
  }

  return `${total.toString()} ${unit}`
}

/**
 * Whether this workspace's teams disagree about what an estimate means.
 *
 * True the moment two rows carry two scales, which is the condition under
 * which a workspace-wide total would be meaningless -- and therefore the
 * condition under which the screen has to explain why it is not showing one.
 * A workspace whose teams all use points needs no such explanation and does
 * not get one.
 */
export function hasMixedScales(rows: readonly { estimateScale: EstimateScale }[]): boolean {
  return new Set(rows.map((row) => row.estimateScale)).size > 1
}

/** The five workflow-state categories, in board order. The axis, defined once. */
export const STATE_CATEGORIES: readonly WorkflowStateCategory[] = [
  'BACKLOG',
  'UNSTARTED',
  'STARTED',
  'COMPLETED',
  'CANCELED',
]

export const STATE_LABELS: Readonly<Record<WorkflowStateCategory, string>> = {
  BACKLOG: 'Backlog',
  UNSTARTED: 'Todo',
  STARTED: 'In progress',
  COMPLETED: 'Done',
  CANCELED: 'Canceled',
}

/**
 * Every category, including the ones holding nothing.
 *
 * The server omits empty categories, because filling them there would put the
 * axis in eleven statements instead of in one place. This is that one place: a
 * category with no issues is a zero on the chart rather than a missing bar,
 * and a chart missing a bar reads as a chart with fewer categories.
 */
export function denseStateMix(
  mix: readonly StateCategoryCount[],
): { category: WorkflowStateCategory; label: string; issues: number }[] {
  const counted = new Map(mix.map((count) => [count.category, count.issues]))

  return STATE_CATEGORIES.map((category) => ({
    category,
    label: STATE_LABELS[category],
    issues: counted.get(category) ?? 0,
  }))
}

/**
 * The five priority levels, low number first.
 *
 * 0 is "no priority" and NOT the lowest one -- the column stores 0 for
 * untriaged, which is why the issue list sorts by `NULLIF(priority, 0)`. It is
 * a bucket here rather than an exclusion, because it is usually the largest.
 */
export const PRIORITY_LABELS = ['No priority', 'Urgent', 'High', 'Medium', 'Low'] as const

export function densePriorityMix(
  mix: readonly PriorityCount[],
): { priority: number; label: string; issues: number }[] {
  const counted = new Map(mix.map((count) => [count.priority, count.issues]))

  return PRIORITY_LABELS.map((label, priority) => ({
    priority,
    label,
    issues: counted.get(priority) ?? 0,
  }))
}

/** The tallest bar a chart has to fit, never zero so nothing divides by it. */
export function peak(values: readonly number[]): number {
  return Math.max(1, ...values)
}

/**
 * The busiest day in the window, for a summary that stands in for the chart.
 *
 * A chart's text alternative cannot be 180 rows, and a screen reader user
 * reading 180 rows learns less than one sentence would tell them. This is that
 * sentence's subject.
 */
export function busiestDay(series: readonly ThroughputDay[]): ThroughputDay | null {
  return series.reduce<ThroughputDay | null>(
    (best, day) => (best === null || day.completed > best.completed ? day : best),
    null,
  )
}
