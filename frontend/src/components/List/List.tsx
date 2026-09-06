import type { ReactNode } from 'react'

import { cx } from '../cx'
import styles from './List.module.css'

export interface ListProps {
  /** Names the list -- "Issues in Cycle 12". Screen readers read it before the count. */
  label: string
  children: ReactNode
  className?: string
}

/**
 * A dense list of rows.
 *
 * A real `<ul>`. The list role is what makes a screen reader announce "list, 47
 * items" and offer to skip past it -- a stack of `<div>`s announces nothing and
 * has to be walked one row at a time.
 *
 * `role="list"` is stated explicitly because removing the bullets with
 * `list-style: none` also removes the list semantics in Safari; that is a
 * genuine WebKit behaviour, not a superstition, and the attribute is the
 * documented workaround.
 */
export function List({ label, children, className }: ListProps) {
  return (
    <ul role="list" aria-label={label} className={cx(styles.list, className)}>
      {children}
    </ul>
  )
}

export interface ListRowProps {
  /** Hover feedback. Set when the row leads somewhere. */
  interactive?: boolean
  /** The row is the current one -- keyboard cursor, or open in a split view. */
  selected?: boolean
  children: ReactNode
  className?: string
}

/**
 * One row.
 *
 * Presentational, and deliberately not clickable itself. A row that is a button
 * cannot contain the buttons and links a real issue row needs -- nested
 * interactive elements are invalid and unreachable by keyboard. The pattern
 * that works is a link on the row's title stretched over the row with an
 * absolutely positioned `::after`; the row is `position: relative` here so that
 * the caller can do exactly that.
 *
 * `selected` is presentation only. Whatever owns the selection must also say so
 * semantically -- `aria-current` on the link, or `aria-selected` in a listbox --
 * because a tinted background is not information.
 */
export function ListRow({
  interactive = false,
  selected = false,
  children,
  className,
}: ListRowProps) {
  return (
    <li
      className={cx(
        styles.row,
        interactive && styles.interactive,
        selected && styles.selected,
        className,
      )}
    >
      {children}
    </li>
  )
}

export interface ListRowSlotProps {
  children: ReactNode
  className?: string
}

/** The row's title column: takes the free space and truncates. */
export function ListRowMain({ children, className }: ListRowSlotProps) {
  return <div className={cx(styles.main, className)}>{children}</div>
}

/** The row's trailing column: labels, assignee, dates. Never shrinks. */
export function ListRowMeta({ children, className }: ListRowSlotProps) {
  return <div className={cx(styles.meta, className)}>{children}</div>
}
