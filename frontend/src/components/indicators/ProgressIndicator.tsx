import { cx } from '../cx'
import styles from './indicators.module.css'

/**
 * The same rounded square as the status glyphs, drawn as a track that fills
 * clockwise from top-centre.
 *
 * Written as a `<path>` rather than a `<rect rx>` for one reason:
 * `pathLength` is what makes the arithmetic trivial. Declaring
 * `pathLength="100"` tells the renderer to treat the outline as exactly 100
 * units long whatever its real geometry is, so `stroke-dasharray: 62 100`
 * draws 62% -- no measuring the path, no recomputing when the radius changes,
 * and no `2 * Math.PI * r` that silently stops being true the moment the shape
 * is not a circle.
 *
 * Starting the subpath at (8, 2.6) rather than a corner puts 0% at twelve
 * o'clock, which is where people expect a gauge to start.
 */
const TRACK =
  'M8 2.6h2.2a3.2 3.2 0 0 1 3.2 3.2v4.4a3.2 3.2 0 0 1-3.2 3.2H5.8a3.2 3.2 0 0 1-3.2-3.2V5.8a3.2 3.2 0 0 1 3.2-3.2Z'

export interface ProgressIndicatorProps {
  /** Completed units. Clamped against `total`; negatives read as zero. */
  value: number
  /** Total units. A total of zero renders an empty track rather than dividing by it. */
  total: number
  /**
   * What is being measured -- "Cycle 12", "Payments migration". Becomes part
   * of the accessible name, because "62 percent" on its own tells a screen
   * reader user nothing about 62 percent of what.
   */
  label: string
  /** Render "8/13" beside the glyph. */
  showLabel?: boolean
  className?: string
}

/**
 * Cycle and project completion.
 *
 * `role="progressbar"` rather than the `role="img"` the other indicators use:
 * this one has a *value*, and the progressbar role is what carries a value to
 * assistive technology. `aria-valuetext` overrides the percentage the reader
 * would otherwise compute, so it says "8 of 13 issues" -- which is the number
 * the user is actually tracking.
 *
 * Not `<progress>`: the native element cannot be drawn as a ring in any
 * cross-browser way, and its shadow DOM is styleable only through vendor
 * pseudo-elements that differ per engine. The role and the ARIA values give us
 * everything the native element would have contributed to the a11y tree.
 */
export function ProgressIndicator({
  value,
  total,
  label,
  showLabel = false,
  className,
}: ProgressIndicatorProps) {
  const safeTotal = Math.max(0, Math.trunc(total))
  const safeValue = Math.min(Math.max(0, Math.trunc(value)), safeTotal)
  const percent = safeTotal === 0 ? 0 : (safeValue / safeTotal) * 100

  return (
    <span
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={safeTotal}
      aria-valuenow={safeValue}
      aria-valuetext={`${String(safeValue)} of ${String(safeTotal)}`}
      className={cx(styles.indicator, styles.progress, className)}
    >
      <svg
        viewBox="0 0 16 16"
        className={styles.glyph}
        strokeWidth={2}
        strokeLinecap="round"
        aria-hidden="true"
        focusable="false"
      >
        <path d={TRACK} className={styles.progressTrack} />
        {percent > 0 && (
          <path
            d={TRACK}
            pathLength={100}
            strokeDasharray={`${String(percent)} 100`}
            className={styles.progressValue}
          />
        )}
      </svg>
      {showLabel && (
        <span className={styles.label}>
          {safeValue}/{safeTotal}
        </span>
      )}
    </span>
  )
}
