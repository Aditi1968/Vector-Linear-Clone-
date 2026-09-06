import type { RouteObject } from 'react-router-dom'

import { AppLayout } from '../layout'
import { IssueDetailPage, IssueListPage } from '../../features/issues'
import { NotFound } from './NotFound'
import { ROUTE_SEGMENTS, WORKSPACE_SLUG_PARAM } from './paths'
import { RouteError } from './RouteError'
import { WorkspaceEntry } from './WorkspaceEntry'

/**
 * The route table.
 *
 * One parent route wrapping every page, with children declared as *relative*
 * segments. That shape is what made the workspace segment a two-line change
 * rather than a sweep: the parent's path became `/:workspaceSlug`,
 * `useAppPaths()` began reading the param, and no feature component moved.
 * The children are written as `issues` and `issues/:issueId` rather than
 * `/issues` and `/issues/:issueId`, so React Router resolves them against
 * whatever the parent turns out to be, and components never see either form
 * because they navigate through `useAppPaths()`.
 *
 * `/` is a separate route and not an index of the parent, because it has no
 * workspace to be an index OF -- the shell's own links need a slug. It
 * resolves one and redirects; see ./WorkspaceEntry.
 *
 * Exported as data rather than JSX elements so tests can mount a subtree
 * with a memory router without booting the whole application.
 */
export const routes: RouteObject[] = [
  {
    // Exactly `/`, so it cannot shadow `/:workspaceSlug`. A slug that
    // happens to be spelled like a segment of this app's own URLs is still
    // just a slug: `/issues/issues` is a real workspace's issue list.
    index: true,
    element: <WorkspaceEntry />,
    errorElement: <RouteError />,
  },
  {
    path: `/:${WORKSPACE_SLUG_PARAM}`,

    // The shell mounts once here, as the router's root element, so chrome
    // survives navigation between children instead of remounting per route.
    element: <AppLayout />,

    // Without this, React Router's own fallback renders the caught error's
    // stack trace into the production bundle. See ./RouteError.
    errorElement: <RouteError />,

    children: [
      {
        // A workspace with no page named is its issue list, which is the
        // product's front door. Rendered rather than redirected: there is
        // nothing to resolve here, so a redirect would only put a URL in
        // history that immediately leaves again.
        index: true,
        element: <IssueListPage />,
      },
      {
        path: ROUTE_SEGMENTS.issues,
        element: <IssueListPage />,
      },
      {
        path: ROUTE_SEGMENTS.issueDetail,
        element: <IssueDetailPage />,
      },
      {
        // Inside the parent, so an unknown URL still renders the shell.
        path: '*',
        element: <NotFound />,
      },
    ],
  },
]
