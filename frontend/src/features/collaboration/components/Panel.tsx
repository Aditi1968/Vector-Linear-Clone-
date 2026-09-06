import { useId } from 'react'
import type { ReactNode } from 'react'

import { ErrorState, Skeleton } from '../../../components'
import styles from '../collaboration.module.css'

export interface PanelProps {
  /** "Comments", "Labels". Becomes the section's accessible name. */
  title: string
  /** Rendered inside the heading, so the name reads "Comments 3". */
  count?: number
  /** The panel's affordance, right-aligned in the header. */
  action?: ReactNode
  /** The first load, with nothing on screen yet. Not a `fetchMore`. */
  isLoading?: boolean
  /** A failure concerning the whole panel. Safe to show: see `describeError`. */
  errorMessage?: string | null
  onRetry?: () => void
  /**
   * The last thing that happened, announced.
   *
   * The live region below is rendered whether or not there is anything in it,
   * because a region added to the DOM at the same moment as its text is
   * frequently not announced at all -- the assistive technology has to be
   * watching it before it changes. Panels set this to a sentence after a
   * mutation and clear it when the user starts something new.
   */
  status?: string | null
  children: ReactNode
}

/**
 * The frame every collaboration panel shares.
 *
 * A real `<section>` with an accessible name, so the four of them are
 * landmarks a screen-reader user can jump between rather than four unlabelled
 * runs of content down one page.
 *
 * The heading is an `<h2>`. These are top-level sections of an issue view
 * whose title is the page's `<h1>`; a panel does not know what A6 renders
 * around it, and a level that is occasionally one step off is a far smaller
 * problem than four sections with no heading at all.
 *
 * Loading and error are handled here rather than in each panel because all
 * four want the same two screens, and four copies of "if loading, skeleton"
 * is four places for one of them to grow a different answer.
 */
export function Panel({
  title,
  count,
  action,
  isLoading = false,
  errorMessage = null,
  onRetry,
  status = null,
  children,
}: PanelProps) {
  const titleId = useId()

  return (
    <section className={styles.panel} aria-labelledby={titleId}>
      <div className={styles.panelHeader}>
        <h2 id={titleId} className={styles.panelTitle}>
          {title}
          {count !== undefined && <span className={styles.count}>{count}</span>}
        </h2>
        {action}
      </div>

      {/* Always mounted -- see the note on `status`. The loading sentence goes
        * through the same region because `Skeleton` is `aria-hidden`: it is a
        * picture of text that does not exist yet, and announcing the bars
        * themselves would give a screen-reader user blank items to walk. */}
      <div role="status" aria-live="polite" className={styles.announcement}>
        {isLoading ? `Loading ${title.toLowerCase()}` : status}
      </div>

      {isLoading ? (
        <div className={styles.loading}>
          <Skeleton />
          <Skeleton width="70%" />
        </div>
      ) : errorMessage !== null ? (
        <ErrorState
          title={`Could not load ${title.toLowerCase()}`}
          description={errorMessage}
          onRetry={onRetry}
        />
      ) : (
        children
      )}
    </section>
  )
}
