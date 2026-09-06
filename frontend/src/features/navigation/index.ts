/**
 * The rail's navigation.
 *
 * `SidebarNav` is what the shell mounts. `NavRow` and the item arrays are
 * exported because a screen that grows its own secondary navigation should
 * use the same row rather than a second one that drifts from it.
 */
export { SidebarNav } from './SidebarNav'
export type { SidebarNavProps } from './SidebarNav'

export { NavRow } from './NavRow'
export type { NavRowProps } from './NavRow'

export { primaryNavigationItems, workspaceNavigationItems } from './navigationItems'
export type { NavigationItem } from './navigationItems'
