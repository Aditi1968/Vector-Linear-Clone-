/**
 * The issues feature's public surface.
 *
 * One screen behind two names. `/:workspaceSlug/issues` and
 * `/:workspaceSlug/issues/:issueId` are the same component -- the list stays
 * mounted and the URL decides whether an inspector is open beside it -- so
 * the route table's two entries deliberately resolve to one function.
 *
 * The names are kept because the route table (`src/app/routes/routes.tsx`)
 * is not this feature's file to rewrite:
 *
 *     import { IssueDetailPage, IssueListPage } from '../../features/issues'
 *
 *     { path: ROUTE_SEGMENTS.issues,      element: <IssueListPage /> }
 *     { path: ROUTE_SEGMENTS.issueDetail, element: <IssueDetailPage /> }
 *
 * Whoever collapses those two entries can import `IssuesScreen` instead.
 */

export {
  IssuesScreen,
  IssuesScreen as IssueDetailPage,
  IssuesScreen as IssueListPage,
} from './pages/IssuesScreen'
