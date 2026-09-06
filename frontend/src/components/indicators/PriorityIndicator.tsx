import type { ReactElement } from 'react'

import { cx } from '../cx'
import styles from './indicators.module.css'

/**
 * The five priority levels, named rather than numbered.
 *
 * Deliberately *not* the server's integer. The mapping from
 * `Issue.priority` to a level is a frontend convention that already has an
 * owner (`features/issues/lib/priority.ts`, which documents why 0 means
 * "unset" and 1 means "urgent"), and a primitive that re-derived it would be
 * a second source of truth for the same invented rule -- the kind that stays
 * agreed for exactly as long as nobody edits either copy.
 */
export type PriorityLevel = 'none' | 'low' | 'medium' | 'high' | 'urgent'

const LEVEL_NAMES: Record<PriorityLevel, string> = {
  none: 'No priority',
  low: 'Low',
  medium: 'Medium',
  high: 'High',
  urgent: 'Urgent',
}

/** Bar geometry: three slots on a shared baseline, ascending. */
const SLOTS = [
  { x: 2.7, y: 9.2, height: 3.4 },
  { x: 6.7, y: 6.8, height: 5.8 },
  { x: 10.7, y: 4.4, height: 8.2 },
] as const

function bars(filled: number): ReactElement {
  return (
    <>
      {SLOTS.map((slot, index) =>
        index < filled ? (
          <rect
            key={slot.x}
            x={slot.x}
            y={slot.y}
            width={2.6}
            height={slot.height}
            rx={1}
            className={styles.solid}
          />
        ) : (
          /* An empty slot is a dot on the baseline, not a faded bar. Faded
           * bars encode magnitude in opacity, which disappears at 14px and in
           * forced-colours mode; a dot is a different shape, and counting
           * shapes works in greyscale. */
          <circle
            key={slot.x}
            cx={slot.x + 1.3}
            cy={11.45}
            r={1.15}
            className={styles.solid}
          />
        ),
      )}
    </>
  )
}

const GLYPHS: Record<PriorityLevel, ReactElement> = {
  /* A single rule. "Unset" should read as an absence, and an absence is one
   * mark, not three faint ones. */
  none: <path d="M4.4 8h7.2" strokeWidth={1.7} strokeLinecap="round" />,
  low: bars(1),
  medium: bars(2),
  high: bars(3),
  /**
   * Urgent is the only *filled* glyph in the whole set, which is what makes it
   * findable in a scrolling list without relying on red.
   *
   * The exclamation is a hole, not an overlaid stroke: one path, `evenodd`, so
   * the counter is genuinely transparent and the glyph works on any surface
   * and in either theme. Drawing the mark in the background colour instead
   * would break the moment the row is hovered, selected, or dark.
   */
  urgent: (
    <path
      fillRule="evenodd"
      d="M5.8 2.6h4.4a3.2 3.2 0 0 1 3.2 3.2v4.4a3.2 3.2 0 0 1-3.2 3.2H5.8a3.2 3.2 0 0 1-3.2-3.2V5.8a3.2 3.2 0 0 1 3.2-3.2ZM7.05 4.5h1.9v4.4h-1.9ZM7.05 9.9h1.9v1.7h-1.9Z"
      className={styles.solid}
    />
  ),
}

export interface PriorityIndicatorProps {
  level: PriorityLevel
  /** Override the displayed and announced name (e.g. to include the raw value). */
  name?: string
  showLabel?: boolean
  className?: string
}

/** See `StatusIndicator` for why the name lives on the wrapper as `role="img"`. */
export function PriorityIndicator({
  level,
  name,
  showLabel = false,
  className,
}: PriorityIndicatorProps) {
  const levelName = name ?? LEVEL_NAMES[level]

  return (
    <span
      role="img"
      aria-label={`Priority: ${levelName}`}
      data-level={level}
      className={cx(styles.indicator, styles.priority, className)}
    >
      <svg
        viewBox="0 0 16 16"
        className={styles.glyph}
        aria-hidden="true"
        focusable="false"
      >
        {GLYPHS[level]}
      </svg>
      {showLabel && <span className={styles.label}>{levelName}</span>}
    </span>
  )
}
