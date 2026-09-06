import { VisuallyHidden } from '../../../components'
import styles from '../issues.module.css'
import { describePriority } from '../lib/priority'

export interface PriorityBadgeProps {
  value: number
}

/**
 * A priority, shown as the integer the server sent plus this app's name for it.
 *
 * Both halves are on screen on purpose. The integer is what the API has; the
 * name is a convention this frontend invented (see ../lib/priority) and is
 * never shown alone, so nobody reads "High" off a screen and concludes the
 * schema has a `High`.
 *
 * The two visible spans are `aria-hidden` and one hidden string carries the
 * whole thing to assistive technology. Otherwise a screen reader announces
 * "2 High", which is two unexplained fragments; the hidden label says
 * "Priority 2, labelled High by this app".
 */
export function PriorityBadge({ value }: PriorityBadgeProps) {
  const { name, label } = describePriority(value)

  return (
    <span className={styles.priority} data-priority={value} title={label}>
      <span className={styles.priorityValue} aria-hidden="true">
        {value}
      </span>
      {name !== null && (
        <span className={styles.priorityName} aria-hidden="true">
          {name}
        </span>
      )}
      <VisuallyHidden>{label}</VisuallyHidden>
    </span>
  )
}
