import { ChevronLeftIcon, ChevronRightIcon, IconButton, cx } from '../../components'
import { SidebarNav } from '../../features/navigation'
import { AccountMenu } from './AccountMenu'
import { CommandAffordance, SearchAffordance } from './ShellActions'
import { CreateIssueButton } from './CreateIssueButton'
import { SIDEBAR_NAV_ID } from './AppLayout'
import { WorkspaceSwitcher } from './WorkspaceSwitcher'
import styles from './Sidebar.module.css'

export interface SidebarProps {
  collapsed: boolean
  onToggleCollapsed: () => void
}

/**
 * Vector's persistent rail.
 *
 * A `<header>` element, which at this position in the document -- a direct
 * child of the shell, not nested inside `main`, `article`, `aside`, `nav` or
 * `section` -- is the page's `banner` landmark. That is the accurate label
 * for what it holds: workspace identity, global actions, primary navigation
 * and the account. `<aside>` would have been wrong; "complementary" means
 * content tangential to the main content, and a product's primary navigation
 * is not tangential to anything.
 *
 * The landmark structure is banner > navigation, plus main. Each is named,
 * which is what a screen-reader user navigates the shell by.
 *
 * ## Collapsing
 *
 * One piece of state drives two layouts, because they are the same question
 * asked twice. Wide: collapsed means an icon-only rail. Narrow (below
 * `--layout-breakpoint-compact`, where the rail becomes a bar across the
 * top): collapsed means the navigation is not showing. Either way the toggle
 * says what it will do, carries `aria-expanded`, and points `aria-controls`
 * at the region it opens.
 *
 * Labels are hidden with CSS rather than removed from the DOM, so an
 * icon-only row keeps its accessible name and a screen-reader user loses
 * nothing by collapsing the rail.
 *
 * Composition order -- identity, global actions, navigation, account -- is
 * the same in both layouts. Nothing is reordered between them, so tab order
 * always matches what is on screen (WCAG 2.4.3).
 */
export function Sidebar({ collapsed, onToggleCollapsed }: SidebarProps) {
  return (
    <header className={cx(styles.sidebar, collapsed && styles.collapsed)}>
      <div className={styles.head}>
        <WorkspaceSwitcher />

        <IconButton
          size="sm"
          className={styles.toggle}
          icon={collapsed ? <ChevronRightIcon /> : <ChevronLeftIcon />}
          aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          aria-expanded={!collapsed}
          aria-controls={SIDEBAR_NAV_ID}
          onClick={onToggleCollapsed}
        />
      </div>

      <div className={styles.actions}>
        <SearchAffordance />
        <CommandAffordance />
        <CreateIssueButton collapsed={collapsed} />
      </div>

      <SidebarNav id={SIDEBAR_NAV_ID} collapsed={collapsed} />

      <AccountMenu />
    </header>
  )
}
