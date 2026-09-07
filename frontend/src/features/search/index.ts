/**
 * The Search screen.
 *
 *     { path: ROUTE_SEGMENTS.search, element: <SearchPage /> }
 *
 * The query lives in the URL as `?q=`, so `paths.search()` plus a search
 * string is a linkable search -- which is what anything navigating here with
 * a query already typed (the command palette, say) should build.
 *
 * The data layer is ./api.ts and is reachable from `features/search/api`
 * when a test needs a document to mock, but never through this module.
 */
export { SearchPage } from './SearchPage'
