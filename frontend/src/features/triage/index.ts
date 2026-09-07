/**
 * The triage feature's public surface.
 *
 * One route element and nothing else. The route table imports this and knows
 * nothing about how the screen gets its data; the data layer lives in ./api
 * and is reachable from `features/triage/api` when a test needs a document to
 * mock, but never through this module.
 *
 * Wiring, in `src/app/routes/routes.tsx`:
 *
 *     { path: ROUTE_SEGMENTS.triage, element: <TriagePage /> }
 */

export { TriagePage } from './pages/TriagePage'
