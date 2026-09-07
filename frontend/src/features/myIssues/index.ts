/**
 * The My Issues screen.
 *
 *     { path: ROUTE_SEGMENTS.myIssues, element: <MyIssuesPage /> }
 *
 * No data layer of its own: the screen is the workspace issue list from
 * `features/issues` with a client-side filter over it, because the API
 * exposes no assignee argument. See ./MyIssuesPage.tsx.
 */
export { MyIssuesPage } from './MyIssuesPage'
