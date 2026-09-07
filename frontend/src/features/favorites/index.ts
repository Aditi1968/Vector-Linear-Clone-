/**
 * The favorites feature's public surface.
 *
 * One route element and nothing else. The data layer lives in ./api and is
 * reachable from `features/favorites/api` when a test needs a document to
 * mock, but never through this module.
 *
 * Wiring, in `src/app/routes/routes.tsx`:
 *
 *     { path: ROUTE_SEGMENTS.favorites, element: <FavoritesPage /> }
 */

export { FavoritesPage } from './pages/FavoritesPage'
