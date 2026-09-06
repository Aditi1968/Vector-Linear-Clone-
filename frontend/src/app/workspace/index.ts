/**
 * The workspace a screen is currently inside.
 *
 * `useWorkspace()` for anything you *display* -- the workspace's name, the
 * viewer's role, who is signed in. `useWorkspaceSlug()` from `app/routes` for
 * anything you *send*: a query variable is about the address being asked
 * about, and the address bar is the single source for that.
 */
export { WorkspaceProvider, useWorkspace } from './context'
export type { Viewer, WorkspaceContextValue, WorkspaceMembership } from './context'
