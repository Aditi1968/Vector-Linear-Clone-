/**
 * The environments feature's public surface.
 *
 * One route element and nothing else. The data layer lives in ./api and is
 * reachable from `features/environments/api` when a test needs a document to
 * mock -- or when `features/releases` needs to name the target a release went
 * to -- but never through this module.
 *
 * Wiring, in `src/app/routes/routes.tsx`:
 *
 *     { path: ROUTE_SEGMENTS.environments, element: <EnvironmentsPage /> }
 */

export { EnvironmentsPage } from './pages/EnvironmentsPage'
