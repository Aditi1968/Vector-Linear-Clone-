/**
 * The label-groups feature's public surface.
 *
 * One route element and nothing else. The data layer lives in ./api and is
 * reachable from `features/labelGroups/api` when a test needs a document to
 * mock, but never through this module.
 *
 * Wiring, in `src/app/routes/routes.tsx`:
 *
 *     { path: ROUTE_SEGMENTS.labelGroups, element: <LabelGroupsPage /> }
 */

export { LabelGroupsPage } from './pages/LabelGroupsPage'
