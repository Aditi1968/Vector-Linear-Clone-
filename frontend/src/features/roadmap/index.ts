/**
 * The roadmap feature's public surface.
 *
 * One route element and nothing else. The data layer lives in ./api and is
 * reachable from `features/roadmap/api` when a test needs a document to mock,
 * but never through this module.
 *
 * Wiring, in `src/app/routes/routes.tsx`:
 *
 *     { path: ROUTE_SEGMENTS.roadmap, element: <RoadmapPage /> }
 */

export { RoadmapPage } from './pages/RoadmapPage'
