import type { ReactNode } from 'react'

import { cx } from '../cx'
import styles from './GroupHeader.module.css'

export interface GroupHeaderProps {
  /**
   * The group's glyph -- usually the `StatusIndicator` for the state the
   * group is. Decorative here: the name beside it already says what this is,
   * so a second announcement of "In progress" would be noise.
   */
  icon?: ReactNode
  /** What the group is: a state, an assignee, a project. */
  name: ReactNode
  /** How many rows are in it. Rendered in mono so the counts line up. */
  count?: number
  /** Anything that acts on the whole group, at the trailing edge. */
  actions?: ReactNode
  className?: string
}

/**
 * The bar that heads one run of rows in a grouped list.
 *
 * ## It is not a heading element, and that is deliberate
 *
 * A grouped issue list is one list broken into runs, not a document with
 * sections. The accessible structure that works is a `<List>` per group whose
 * `label` is the group's name -- a screen reader then announces "In progress,
 * list, 6 items" and offers to skip the run, which is what a user of a
 * grouped list actually wants. An `<h2>` here on top of that would announce
 * the same words twice and put entries in the heading map that lead nowhere.
 *
 * So the contract is: render this, then render the group's rows in a `List`
 * carrying the same name.
 *
 *     <GroupHeader icon={<StatusIndicator category="started" />}
 *                  name="In progress" count={rows.length} />
 *     <List label="In progress">{rows}</List>
 *
 * The count is `aria-hidden` for the same reason -- the list announces its own
 * item count, and a screen reader that reads "6" and then "6 items" is
 * reading the interface rather than the content.
 */
export function GroupHeader({
  icon,
  name,
  count,
  actions,
  className,
}: GroupHeaderProps) {
  return (
    <div className={cx(styles.header, className)}>
      {icon !== undefined && (
        <span aria-hidden="true" className={styles.icon}>
          {icon}
        </span>
      )}

      <span className={styles.name}>{name}</span>

      {count !== undefined && (
        <span aria-hidden="true" className={styles.count}>
          {count}
        </span>
      )}

      {actions !== undefined && <span className={styles.actions}>{actions}</span>}
    </div>
  )
}
