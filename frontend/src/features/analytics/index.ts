/**
 * The analytics feature's public surface.
 *
 * One route element and nothing else. The data layer lives in ./api and is
 * reachable from `features/analytics/api` when a test needs a document to
 * mock, but never through this module.
 *
 * Wiring, in `src/app/routes/routes.tsx`:
 *
 *     { path: ROUTE_SEGMENTS.analytics, element: <AnalyticsPage /> }
 */

export { AnalyticsPage } from './pages/AnalyticsPage'
