import type { ReactNode } from 'react'

import { AlertIcon } from '../icons'
import { Button } from '../Button'
import { cx } from '../cx'
import styles from './States.module.css'

export interface ErrorStateProps {
  /** What failed, said plainly -- "Could not load issues". */
  title: string
  /** What the user can do. Never the raw exception. */
  description?: string
  /** Shows a retry button when supplied. */
  onRetry?: () => void
  /** Extra actions beside retry. */
  actions?: ReactNode
  /**
   * Technical detail, hidden behind a disclosure.
   *
   * Safe to show only what the server chose to return -- a correlation id, a
   * structured error code. Never an exception message from an unexpected
   * failure: those carry table names, query fragments and file paths, and this
   * component has no way to tell one kind from the other, so the caller must.
   */
  detail?: string
  className?: string
}

/**
 * Something failed and the user is stuck.
 *
 * `role="alert"` is right here where it is wrong on `EmptyState`: an error
 * appearing after an action the user took is exactly the case the role exists
 * for, and interrupting is the correct behaviour when the thing they asked for
 * did not happen.
 */
export function ErrorState({
  title,
  description,
  onRetry,
  actions,
  detail,
  className,
}: ErrorStateProps) {
  return (
    <div role="alert" className={cx(styles.state, styles.error, className)}>
      <div className={styles.glyph}>
        <AlertIcon />
      </div>
      <p className={styles.title}>{title}</p>
      {description !== undefined && (
        <p className={styles.description}>{description}</p>
      )}

      {(onRetry !== undefined || actions !== undefined) && (
        <div className={styles.actions}>
          {onRetry !== undefined && (
            <Button variant="secondary" onClick={onRetry}>
              Try again
            </Button>
          )}
          {actions}
        </div>
      )}

      {detail !== undefined && (
        <details className={styles.details}>
          <summary className={styles.summary}>Technical details</summary>
          <div className={styles.detailsBody}>{detail}</div>
        </details>
      )}
    </div>
  )
}
