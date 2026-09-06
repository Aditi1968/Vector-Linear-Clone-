import type { ReactElement } from 'react'

import { cx } from '../cx'
import styles from './indicators.module.css'

/**
 * The five workflow-state categories, mirroring the server's
 * `WorkflowStateCategory` enum in lowercase.
 */
export type StatusCategory =
  | 'backlog'
  | 'unstarted'
  | 'started'
  | 'completed'
  | 'canceled'

const CATEGORY_NAMES: Record<StatusCategory, string> = {
  backlog: 'Backlog',
  unstarted: 'Todo',
  started: 'In progress',
  completed: 'Done',
  canceled: 'Canceled',
}

const CATEGORIES = Object.keys(CATEGORY_NAMES) as readonly StatusCategory[]

/**
 * Narrow a wire value (`"IN_PROGRESS"`-style SCREAMING_SNAKE from GraphQL) to
 * a category, or `null` if the server sent something this build has never
 * heard of.
 *
 * `null` rather than a fallback to `backlog`: a category we cannot draw is a
 * schema change, and rendering it as a plausible-looking backlog glyph hides
 * that until someone notices their issues are in the wrong bucket.
 */
export function statusCategoryFrom(value: string): StatusCategory | null {
  const normalised = value.toLowerCase()
  return CATEGORIES.find((category) => category === normalised) ?? null
}

/**
 * Vector's status glyphs.
 *
 * The shared silhouette is a rounded square -- Vector's unit-of-work shape,
 * the same one `IssueIcon` uses -- rather than the segmented ring this product
 * category has converged on. Each category then changes the *shape*, not only
 * the colour:
 *
 *   backlog    dashed outline, empty    -- not yet real
 *   unstarted  solid outline, empty     -- real, untouched
 *   started    washed, solid core       -- something is inside it
 *   completed  washed, check            -- closed successfully
 *   canceled   outline, slash           -- closed, struck out
 *
 * Dashed / hollow / cored / checked / struck are five different silhouettes,
 * so the whole set survives greyscale, and therefore survives every form of
 * colour blindness. Colour is the redundant channel here, never the only one.
 *
 * The wash is `currentColor` at 16%, not a grey: it tints with the category
 * and composites over any surface, in either theme.
 */
const TRACK = 'M5.8 2.6h4.4a3.2 3.2 0 0 1 3.2 3.2v4.4a3.2 3.2 0 0 1-3.2 3.2H5.8a3.2 3.2 0 0 1-3.2-3.2V5.8a3.2 3.2 0 0 1 3.2-3.2Z'

const GLYPHS: Record<StatusCategory, ReactElement> = {
  backlog: (
    <>
      {/* Dashes rather than a lighter stroke: at 14px a 1px hairline and a
        * 1.6px stroke are hard to tell apart, but present-vs-absent segments
        * are unmistakable. */}
      <path d={TRACK} strokeDasharray="2.1 2.3" />
    </>
  ),
  unstarted: <path d={TRACK} />,
  started: (
    <>
      <path d={TRACK} className={styles.wash} />
      <path d={TRACK} />
      {/* A diamond, not a dot: it fills more of the square's optical centre at
        * small sizes, and it cannot be mistaken for the round core other
        * trackers use for the same state. */}
      <path d="M8 5.2 10.8 8 8 10.8 5.2 8Z" className={styles.solid} />
    </>
  ),
  completed: (
    <>
      <path d={TRACK} className={styles.wash} />
      <path d={TRACK} />
      <path d="m5.3 8.1 1.95 1.95 3.5-3.95" />
    </>
  ),
  canceled: (
    <>
      {/* Deliberately unwashed. "Closed and abandoned" should read as emptier
        * than "closed and done", not merely differently coloured. */}
      <path d={TRACK} />
      <path d="M5.6 10.4 10.4 5.6" />
    </>
  ),
}

export interface StatusIndicatorProps {
  category: StatusCategory
  /**
   * The workflow state's own name -- "In review", "Ready to ship". Falls back
   * to the category's generic name, because a team that has not renamed its
   * states still needs the glyph to say something.
   */
  name?: string
  /** Render the name beside the glyph as well as announcing it. */
  showLabel?: boolean
  className?: string
}

/**
 * `role="img"` with an `aria-label` on the wrapper, always.
 *
 * That makes the whole thing one opaque graphic to assistive technology with
 * exactly one name, whether or not a visible label is rendered -- so the
 * announcement does not change shape depending on a presentational prop, and
 * the visible text can never be read out twice.
 */
export function StatusIndicator({
  category,
  name,
  showLabel = false,
  className,
}: StatusIndicatorProps) {
  const stateName = name ?? CATEGORY_NAMES[category]

  return (
    <span
      role="img"
      aria-label={`Status: ${stateName}`}
      data-category={category}
      className={cx(styles.indicator, styles.status, className)}
    >
      <svg
        viewBox="0 0 16 16"
        className={styles.glyph}
        strokeWidth={1.6}
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
        focusable="false"
      >
        {GLYPHS[category]}
      </svg>
      {showLabel && <span className={styles.label}>{stateName}</span>}
    </span>
  )
}
