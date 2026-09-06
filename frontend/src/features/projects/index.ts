/**
 * The projects feature's public surface.
 *
 * Two route elements and nothing else. The route table imports these and
 * knows nothing about how they get their data; the data layer lives in ./api
 * and is reachable from `features/projects/api` when a test needs a document
 * to mock, but never through this module.
 *
 * Wiring, for whoever owns `src/app/routes/routes.tsx`:
 *
 *     { path: ROUTE_SEGMENTS.projects,      element: <ProjectListPage /> }
 *     { path: ROUTE_SEGMENTS.projectDetail, element: <ProjectDetailPage /> }
 */

export { ProjectDetailPage } from './pages/ProjectDetailPage'
export { ProjectListPage } from './pages/ProjectListPage'
