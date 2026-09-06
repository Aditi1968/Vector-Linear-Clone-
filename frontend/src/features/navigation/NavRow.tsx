import type { ReactNode } from 'react'
import { NavLink } from 'react-router-dom'

import { cx } from '../../components'
import styles from './SidebarNav.module.css'

export interface NavRowProps {
  to: string
  /** Match the destination exactly rather than as a prefix. */
  end?: boolean
  /** Decorative glyph. The label carries the meaning. */
  icon?: ReactNode
  label: string
  /** A count shown at the trailing edge -- unread notifications, say. */
  badge?: ReactNode
  /** One step deeper: a team's own sections. */
  nested?: boolean
  className?: string
}

/**
 * One row of the rail.
 *
 * `NavLink` rather than `Link`, because it sets `aria-current="page"` on the
 * active link. That matters more than the styling it also enables: the
 * highlight tells a sighted user where they are, and `aria-current` is the
 * only thing that tells everyone else.
 *
 * The label is always in the DOM, even when the rail is collapsed and the
 * `.label` class clips it to nothing. Hiding it with `display: none` would
 * strip the link's accessible name and leave a screen-reader user with a row
 * announced as "link"; clipping keeps the name and removes only the pixels.
 */
export function NavRow({
  to,
  end = false,
  icon,
  label,
  badge,
  nested = false,
  className,
}: NavRowProps) {
  return (
    <NavLink
      to={to}
      end={end}
      className={({ isActive }) =>
        cx(styles.link, nested && styles.nested, isActive && styles.linkActive, className)
      }
    >
      {icon !== undefined && <span className={styles.icon}>{icon}</span>}
      <span className={styles.label}>{label}</span>
      {badge}
    </NavLink>
  )
}
