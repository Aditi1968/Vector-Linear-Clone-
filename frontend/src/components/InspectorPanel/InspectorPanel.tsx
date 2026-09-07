import type { ReactNode } from 'react'

import { cx } from '../cx'
import styles from './InspectorPanel.module.css'

export interface InspectorPanelProps {
  /**
   * Names the region -- "Issue ENG-142".
   *
   * Required, because this is a `complementary` landmark and an unnamed
   * landmark is worse than none: a screen reader offers it in the landmark
   * list as "complementary" with nothing to distinguish it from any other.
   */
  label: string
  /** The head row: usually a status glyph, the issue key, and a close button. */
  header?: ReactNode
  children: ReactNode
  className?: string
}

/**
 * The panel that opens beside a list.
 *
 * An `<aside>`, which is the `complementary` landmark -- content related to
 * the main content and meaningful on its own. That is the accurate label for
 * a detail panel showing the row you selected, and it is what lets a screen
 * reader user jump between the list and the panel without walking through
 * either.
 *
 * ## What it does not do
 *
 * It does not manage focus, trap it, or close on Escape, because it is not a
 * dialog -- it opens beside the list rather than over it, the list stays
 * usable, and stealing focus to a panel the user opened by arrowing through
 * rows would take them out of the list they are still reading. A panel that
 * genuinely needs to be modal wants `Dialog`.
 *
 * It also does not own its own scroll or stickiness. Where the panel sits and
 * how tall it is are the *screen's* layout decisions -- the issues screen
 * makes it sticky above 62rem and stacks it below -- and a panel that pinned
 * itself would have to be fought by every screen that wants it anywhere else.
 */
export function InspectorPanel({
  label,
  header,
  children,
  className,
}: InspectorPanelProps) {
  return (
    <aside aria-label={label} className={cx(styles.panel, className)}>
      {header !== undefined && <div className={styles.header}>{header}</div>}

      <div className={styles.body}>{children}</div>
    </aside>
  )
}
