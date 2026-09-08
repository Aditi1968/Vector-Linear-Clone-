/**
 * The semantic-search feature's public surface.
 *
 * One route element and nothing else. The data layer lives in ./api and is
 * reachable from `features/semanticSearch/api` when a test needs a document to
 * mock, but never through this module.
 *
 * Wiring, in `src/app/routes/routes.tsx`:
 *
 *     { path: ROUTE_SEGMENTS.semanticSearch, element: <SemanticSearchPage /> }
 *
 * Distinct from `features/search`, which owns `/search` and sends the hybrid
 * `search` field. This one sends `issueDuplicateSuggestions` -- the
 * embeddings-only field -- and `embeddingIndexingState`, which is what lets it
 * tell an empty answer from an unbuilt index. See ./pages/SemanticSearchPage.tsx.
 */

export { SemanticSearchPage } from './pages/SemanticSearchPage'
