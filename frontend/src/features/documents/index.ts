/**
 * The documents feature's public surface.
 *
 * One route element and nothing else. The data layer lives in ./api and is
 * reachable from `features/documents/api` when a test needs a document to
 * mock, but never through this module.
 *
 * Wiring, in `src/app/routes/routes.tsx`:
 *
 *     { path: ROUTE_SEGMENTS.documents, element: <DocumentsPage /> }
 */

export { DocumentsPage } from './pages/DocumentsPage'
