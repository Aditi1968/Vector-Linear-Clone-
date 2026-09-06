import type { ReactNode } from 'react'

import { cx } from '../cx'
import styles from './States.module.css'

export interface EmptyStateProps {
  /** A decorative glyph. Omit it rather than reaching for a generic box icon. */
  icon?: ReactNode
  /** What is not here, in the user's words -- "No issues in this cycle". */
  title: string
  /** Why, or what to do about it. One or two sentences. */
  description?: string
  /** The single next action. Two at most. */
  actions?: ReactNode
  className?: string
}

/**
 * There is nothing here, and that is fine.
 *
 * No ARIA role. An empty list is not an alert and must not interrupt: the list
 * it replaces already has an accessible name, and a screen reader that reaches
 * this reads the heading and the sentence in order like any other content.
 * `role="status"` here would announce "no issues" over the top of whatever the
 * user was in the middle of.
 */
export function EmptyState({
  icon,
  title,
  description,
  actions,
  className,
}: EmptyStateProps) {
  return (
    <div className={cx(styles.state, className)}>
      {icon !== undefined && <div className={styles.glyph}>{icon}</div>}
      <p className={styles.title}>{title}</p>
      {description !== undefined && (
        <p className={styles.description}>{description}</p>
      )}
      {actions !== undefined && <div className={styles.actions}>{actions}</div>}
    </div>
  )
}
