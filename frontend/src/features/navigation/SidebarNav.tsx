import { NavLink } from 'react-router-dom'

import { cx } from '../../components'
import { useAppPaths } from '../../app/routes'
import { primaryNavigationItems } from './navigationItems'
import styles from './SidebarNav.module.css'

/**
 * The sidebar's primary navigation.
 *
 * `<nav>` with a label, so it is a navigation landmark a screen reader user
 * can jump straight to. The label is needed even though there is only one
 * landmark today: an unlabelled `<nav>` is announced as bare "navigation",
 * and the second one to appear -- a breadcrumb, a footer -- makes the pair
 * indistinguishable. Naming it now costs nothing and does not have to be
 * remembered later.
 *
 * A `<ul>` rather than a bare stack of links, so the count is announced ("list
 * of 1 item") and the reader's list-navigation commands work. `role="list"`
 * is stated explicitly because Safari drops the implicit list role when
 * `list-style: none` is applied -- which this stylesheet does.
 *
 * `NavLink` rather than `Link`: it sets `aria-current="page"` on the active
 * link itself. That matters more than the styling it also enables -- the
 * highlight tells a sighted user where they are, and `aria-current` is the
 * only thing that tells everyone else.
 */
export function SidebarNav() {
  const paths = useAppPaths()

  return (
    <nav className={styles.nav} aria-label="Main">
      <ul className={styles.list} role="list">
        {primaryNavigationItems.map((item) => (
          <li key={item.id}>
            <NavLink
              to={item.to(paths)}
              end={item.end ?? false}
              className={({ isActive }) =>
                cx(styles.link, isActive && styles.linkActive)
              }
            >
              <span className={styles.icon}>{item.icon}</span>
              <span className={styles.label}>{item.label}</span>
            </NavLink>
          </li>
        ))}
      </ul>
    </nav>
  )
}
