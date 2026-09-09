import styles from '../projects.module.css'

export interface ProgressBarProps {
  /** Completed units. Clamped against `total`; negatives read as zero. */
  value: number
  /** Total units. A total of zero renders an empty track rather than dividing by it. */
  total: number
  /**
   * What is being measured -- "Beta: closed issues". Becomes the accessible
   * name, because "62 percent" on its own says nothing about 62 percent of
   * what.
   */
  label: string
}

/**
 * Every progress reading in the product except one.
 *
 * A flat bar over a hairline track, with the percentage set in mono beside
 * it. Deliberately not a dial: a ring is right where one number stands for a
 * whole thing -- a project's health -- and is decoration everywhere else. One
 * ring in the product is a reading; six are a texture.
 *
 * The a11y contract is `ProgressIndicator`'s, unchanged, because callers that
 * moved from it must not lose anything: `role="progressbar"` carries a value
 * where `role="img"` would not, and `aria-valuetext` overrides the percentage
 * the reader would compute with the counts the user is actually tracking. The
 * bar itself is `aria-hidden` -- it is a picture of the number beside it.
 *
 * ponytail: one segment, not the two the direction draws. The second segment
 * is "in progress", and the issue selections behind every call site carry
 * `completedAt` and nothing else -- there is no started/unstarted signal to
 * split the remainder by. Add the segment when the selection carries a state
 * category; drawing it from nothing would be the data slop the flat bar
 * exists to avoid.
 */
export function ProgressBar({ value, total, label }: ProgressBarProps) {
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
      className={styles.barRow}
    >
      <span className={styles.barTrack} aria-hidden="true">
        <span className={styles.barDone} style={{ inlineSize: `${String(percent)}%` }} />
      </span>
      <span className={styles.barValue}>{percent}%</span>
    </span>
  )
}
