import { Navigate } from 'react-router-dom'
import type { RouteObject } from 'react-router-dom'

import { AppLayout } from '../layout'
import { IssueDetailPage, IssueListPage } from '../../features/issues'
import { NotFound } from './NotFound'
import { appPaths, ROUTE_SEGMENTS } from './paths'
import { RouteError } from './RouteError'

/**
 * The route table.
 *
 * One parent route wrapping every page, with children declared as *relative*
 * segments. That shape is the answer to "how does `/:workspaceSlug` get
 * added later without rewriting feature components":
 *
 *   - the parent's `path` changes from `'/'` to `'/:workspaceSlug'`
 *   - `createAppPaths` in ./paths.ts is called with that slug, via
 *     `useAppPaths()`
 *
 * and nothing else moves. The children below are already written as
 * `issues` and `issues/:issueId` rather than `/issues` and
 * `/issues/:issueId`, so React Router resolves them against whatever the
 * parent turns out to be. Components never see either form, because they
 * navigate through `useAppPaths()`.
 *
 * Exported as data rather than JSX elements so tests can mount a subtree
 * with a memory router without booting the whole application.
 */
export const routes: RouteObject[] = [
  {
    path: '/',

    // The shell mounts once here, as the router's root element, so chrome
    // survives navigation between children instead of remounting per route.
    element: <AppLayout />,

    // Without this, React Router's own fallback renders the caught error's
    // stack trace into the production bundle. See ./RouteError.
    errorElement: <RouteError />,

    children: [
      {
        // `/` is not a page. The issue list is the product's front door, so
        // the root redirects to it -- `replace` so that the redirect does not
        // sit in history and trap the back button on the entry URL.
        index: true,
        element: <Navigate to={appPaths.issues()} replace />,
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
