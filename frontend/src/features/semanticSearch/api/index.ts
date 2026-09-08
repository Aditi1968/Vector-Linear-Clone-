/**
 * The semantic-search data adapter.
 *
 * The boundary the screen is written against. It imports hooks and types from
 * here and never a document or an Apollo hook. The documents are exported too,
 * and only from this module, because mocking a response requires the exact
 * document that produced it.
 */

export { useIndexState, useSemanticMatches } from './queries'
export type { UseIndexStateResult, UseSemanticMatchesResult } from './queries'

export { useEmbeddingRefresh } from './mutations'
export type { UseEmbeddingRefreshResult } from './mutations'

export {
  EmbeddingIndexStateDocument,
  EmbeddingsRefreshDocument,
  SemanticIssueMatchesDocument,
} from './documents'

export type { IndexState, SemanticMatch } from './types'
