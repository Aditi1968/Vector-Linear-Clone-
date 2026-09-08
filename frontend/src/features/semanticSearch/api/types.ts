/**
 * The names this feature knows the server contract by.
 *
 * Aliases of generated types, every one. Nothing here declares a field, a
 * scalar or a nullability, so nothing here can disagree with the schema.
 */

import type {
  EmbeddingIndexStateQuery,
  SemanticIssueMatchesQuery,
} from '../../../generated/operations'

/** One match: an issue, and how near in meaning the server judged it. */
export type SemanticMatch =
  SemanticIssueMatchesQuery['issueDuplicateSuggestions'][number]

/**
 * How much of this workspace's semantic index exists.
 *
 * `enabled` is not a fifth count. It says whether this DEPLOYMENT has an
 * embedder and a job repository at all -- the `vector` extension present, a
 * model wired -- and when it is false the other three are zero without a round
 * trip. A zero beside `enabled: false` therefore means "no index exists and
 * none is being built", which is a completely different sentence from a zero
 * beside `enabled: true`.
 */
export type IndexState = EmbeddingIndexStateQuery['embeddingIndexingState']
