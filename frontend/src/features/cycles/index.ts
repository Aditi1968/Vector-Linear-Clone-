/**
 * The cycles feature's public surface.
 *
 * Two route elements and nothing else. The route table imports these and
 * knows nothing about how they get their data; the data layer lives in ./api
 * and is reachable from `features/cycles/api` when a test needs a document to
 * mock, but never through this module.
 *
 * Wiring, for whoever owns `src/app/routes/routes.tsx`:
 *
 *     { path: ROUTE_SEGMENTS.cycles,      element: <CycleListPage /> }
 *     { path: ROUTE_SEGMENTS.cycleDetail, element: <CycleDetailPage /> }
 */

export { CycleDetailPage } from './pages/CycleDetailPage'
export { CycleListPage } from './pages/CycleListPage'
