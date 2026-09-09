import { cx } from '../../../components'
import type { ClassValue } from '../../../components'
import type { ProjectHealth } from '../lib/projects'
import styles from '../projects.module.css'

/**
 * The health hues. A `Record` keyed by the union, so a fifth health reading is
 * a compile error here rather than an uncoloured ring in production.
 */
const HEALTH_CLASS: Record<ProjectHealth, ClassValue> = {
  'on-track': styles.onTrack,
  'at-risk': styles.atRisk,
  'off-track': styles.offTrack,
  planned: styles.planned,
}

export interface HealthRingProps {
  /** Closed units. Clamped against `total`; negatives read as zero. */
  value: number
  /** Total units. A total of zero draws the bare track rather than dividing by it. */
  total: number
  /** What is being measured. Becomes the accessible name. */
  label: string
  /** Which of the four readings colours the arc. See `projectHealth`. */
  health: ProjectHealth
}

/**
 * A project's health, and the only dial in Vector.
 *
 * One ring, in one place, because a ring earns its shape exactly where a
 * single number stands for a whole thing -- "how is this project doing" --
 * and is data slop anywhere a bar would have said the same in a fifth of the
 * pixels. Every other progress reading in the product is `ProgressBar`.
 *
 * Two channels, not one: the arc's *length* is completion and its *colour* is
 * health, and the colour is never the only carrier -- the state is written out
 * in words a few lines above it in the same `<dl>`.
 *
 * ## Why the SVG is `aria-hidden`
 *
 * A ring is not accessible because it renders. Two `<circle>` elements
 * announce nothing useful, and left exposed they land in the accessible name
 * as noise. So the drawing is hidden and the wrapper carries the whole
 * contract -- `role="progressbar"` with `aria-valuetext`, the same one
 * `ProgressIndicator` and `ProgressBar` carry -- with the percentage also set
 * visibly beside it for anyone who cannot read an arc's length.
 *
 * `pathLength={100}` is the trick `ProgressIndicator` already uses: it
 * declares the outline to be exactly 100 units long whatever its geometry is,
 * so the dash array is the percentage and there is no `2 * Math.PI * r` to
 * drift when the radius changes. `rotate(-90)` puts 0% at twelve o'clock,
 * which is where a person expects a gauge to start.
 */
export function HealthRing({ value, total, label, health }: HealthRingProps) {
  const safeTotal = Math.max(0, Math.trunc(total))
  const safeValue = Math.min(Math.max(0, Math.trunc(value)), safeTotal)
  const percent = safeTotal === 0 ? 0 : Math.round((safeValue / safeTotal) * 100)

  return (
    <span
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={safeTotal}
      aria-valuenow={safeValue}
      aria-valuetext={`${String(safeValue)} of ${String(safeTotal)}`}
      className={styles.ringRow}
    >
      <svg
        viewBox="0 0 36 36"
        className={cx(styles.ring, HEALTH_CLASS[health])}
        strokeWidth={4}
        fill="none"
        aria-hidden="true"
        focusable="false"
      >
        <circle className={styles.ringTrack} cx="18" cy="18" r="14" />
        <circle
          className={styles.ringArc}
          cx="18"
          cy="18"
          r="14"
          pathLength={100}
          strokeLinecap="round"
          strokeDasharray={`${String(percent)} 100`}
          transform="rotate(-90 18 18)"
        />
      </svg>
      <span className={styles.ringValue}>{percent}%</span>
    </span>
  )
}
