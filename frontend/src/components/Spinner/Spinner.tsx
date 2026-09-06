import { cx } from '../cx'
import styles from './Spinner.module.css'

export interface SpinnerProps {
  /**
   * What is being waited for -- "Loading issues". Supplying it makes the
   * spinner a live region that announces itself; omitting it makes it silent
   * decoration.
   *
   * Omit it whenever something nearby already says the same thing (a button
   * whose label changed to "Saving...", a skeleton list with its own status).
   * Two elements announcing one wait is worse than one.
   */
  label?: string
  className?: string
}

/**
 * An indeterminate wait.
 *
 * Sized in `em` like the icons, so it matches the text or the button it sits
 * in without a size prop.
 *
 * `role="status"` rather than `role="alert"`: a load starting is not an
 * interruption, and `status` is polite -- it waits for the screen reader to
 * finish its current sentence instead of cutting across it.
 */
export function Spinner({ label, className }: SpinnerProps) {
  const isLabelled = label !== undefined

  return (
    <svg
      viewBox="0 0 16 16"
      className={cx(styles.spinner, className)}
      strokeWidth={2}
      strokeLinecap="round"
      role={isLabelled ? 'status' : undefined}
      aria-label={label}
      aria-hidden={isLabelled ? undefined : true}
      focusable="false"
    >
      <circle cx="8" cy="8" r="6" className={styles.track} />
      {/* `pathLength` normalises the circumference to 100, so the arc length is
        * written as a percentage instead of a multiple of 2*pi*r that has to be
        * recomputed whenever the radius changes. */}
      <circle cx="8" cy="8" r="6" pathLength={100} strokeDasharray="26 100" />
    </svg>
  )
}
