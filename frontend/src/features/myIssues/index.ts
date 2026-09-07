/**
 * The My Issues screen.
 *
 *     { path: ROUTE_SEGMENTS.myIssues, element: <MyIssuesPage /> }
 *
 * No data layer of its own: the screen is the issue list from
 * `features/issues`, asked for with `filter: { assigneeId }`. See
 * ./MyIssuesPage.tsx.
 */
export { MyIssuesPage } from './MyIssuesPage'
