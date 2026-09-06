import { SidebarNav } from '../../features/navigation'
import { CreateIssueButton } from './CreateIssueButton'
import { SearchAffordance } from './SearchAffordance'
import { WorkspaceBadge } from './WorkspaceBadge'
import styles from './Sidebar.module.css'

/**
 * Vector's persistent sidebar.
 *
 * A `<header>` element, which at this position in the document -- a direct
 * child of the shell, not nested inside `main`, `article`, `aside`, `nav` or
 * `section` -- is the page's `banner` landmark. That is the accurate label
 * for what it holds: the product identity, the global search entry point and
 * the primary navigation. `<aside>` would have been wrong; "complementary"
 * means content tangential to the main content, and a product's primary
 * navigation is not tangential to anything.
 *
 * The resulting landmark structure is banner > navigation, plus main. Three
 * landmarks, each named, which is what a screen-reader user navigates the
 * shell by.
 *
 * Composition order -- identity, then global actions, then navigation -- is
 * the same in both layouts, so nothing is reordered between them and tab
 * order always matches what is on screen.
 */
export function Sidebar() {
  return (
    <header className={styles.sidebar}>
      <WorkspaceBadge />

      <div className={styles.actions}>
        <SearchAffordance />
        <CreateIssueButton />
      </div>

      <SidebarNav />
    </header>
  )
}
