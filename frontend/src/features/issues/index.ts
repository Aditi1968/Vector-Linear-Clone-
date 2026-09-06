/**
 * The issues feature's public surface.
 *
 * Two route elements and nothing else. The route table imports these and
 * knows nothing about how they get their data; the data layer lives in
 * ./api and is reachable from `features/issues/api` when a test needs a
 * document to mock, but never through this module.
 *
 * Wiring, for whoever owns `src/app/routes/routes.tsx`:
 *
 *     import { IssueDetailPage, IssueListPage } from '../../features/issues'
 *
 *     { path: ROUTE_SEGMENTS.issues,      element: <IssueListPage /> }
 *     { path: ROUTE_SEGMENTS.issueDetail, element: <IssueDetailPage /> }
 */

export { IssueDetailPage } from './pages/IssueDetailPage'
export { IssueListPage } from './pages/IssueListPage'
