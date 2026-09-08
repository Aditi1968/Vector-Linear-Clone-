/**
 * The releases feature's public surface.
 *
 * One route element and nothing else. The data layer lives in ./api and is
 * reachable from `features/releases/api` when a test needs a document to mock
 * -- or when `features/environments` needs the release list to say what was
 * last deployed where -- but never through this module.
 *
 * Wiring, in `src/app/routes/routes.tsx`:
 *
 *     { path: ROUTE_SEGMENTS.releases, element: <ReleasesPage /> }
 */

export { ReleasesPage } from './pages/ReleasesPage'
