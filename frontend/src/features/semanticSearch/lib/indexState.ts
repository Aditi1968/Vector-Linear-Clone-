/**
 * What an empty answer is allowed to say.
 *
 * This is the whole reason `embeddingIndexingState` exists in the schema, and
 * the whole reason this file is a separate module with its own test rather
 * than a ternary in the page. From `SearchService.indexing_state`:
 *
 *     "`suggest_duplicates` answers with an empty list both when nothing is
 *      similar and when nothing is indexed, and on a fresh install it is
 *      always the second -- so every interface built on it has been rendering
 *      'no possible duplicates' over a workspace that has never been
 *      embedded."
 *
 * "Nothing matches" is therefore a claim this screen may make in exactly one
 * situation: the index is enabled, something is in it, and nothing is waiting.
 * Every other shape of `IndexState` gets a sentence that says what is actually
 * true, and none of them says the search found nothing.
 */

import type { IndexState } from '../api'

/** One empty-result explanation: the headline, and the sentence beneath it. */
export interface EmptyAnswer {
  title: string
  description: string
  /**
   * The answer is a genuine "nothing is similar" rather than a gap in the
   * index. Only one branch below sets it, and the screen uses it to decide
   * whether offering "index now" would be beside the point.
   */
  isComplete: boolean
}

/**
 * Explain an empty result honestly, given what the index says about itself.
 *
 * The branches are ordered by how badly each would mislead if it were reported
 * as "nothing matches":
 *
 *   1. **We have not been told.** No state yet -- the query is in flight or it
 *      failed. Claiming anything about coverage here would be inventing it.
 *   2. **Disabled.** This deployment wired no embedder: no `vector`
 *      extension, or no model. Nothing is indexed and nothing ever will be
 *      until that changes, so the empty list says nothing at all about the
 *      workspace's issues.
 *   3. **Nothing indexed.** Enabled, and the index is empty. A fresh install.
 *   4. **Still building.** Some issues are indexed and some are waiting. The
 *      result is real but partial, and an issue in the backlog cannot be found
 *      however well it matches.
 *   5. **Complete.** Enabled, populated, nothing pending. Now -- and only now
 *      -- "nothing is near enough in meaning" is a true sentence.
 *
 * `failed` never suppresses the claim on its own, and that is deliberate: a
 * poison row is a permanent gap, so waiting for it to clear would mean this
 * screen could never say "nothing matches" again. It is reported beside the
 * answer instead -- see `describeCoverage`.
 */
export function explainEmptyAnswer(state: IndexState | null): EmptyAnswer {
  if (state === null) {
    return {
      title: 'No matches to show yet',
      description:
        'The semantic index has not reported how much of this workspace it covers, so there is no way to tell an empty answer from an unbuilt index.',
      isComplete: false,
    }
  }

  if (!state.enabled) {
    return {
      title: 'Semantic search is not available here',
      description:
        'This deployment has no embedding model wired, so no issue has ever been indexed and this search cannot find anything. It is not that nothing matches — nothing has been looked at.',
      isComplete: false,
    }
  }

  if (state.indexed === 0) {
    return {
      title: 'Nothing has been indexed yet',
      description:
        'The index is empty, so an empty result says nothing about this workspace’s issues. Index a batch and search again.',
      isComplete: false,
    }
  }

  if (state.pending > 0) {
    return {
      title: 'No matches among the issues indexed so far',
      description: `${state.pending} ${state.pending === 1 ? 'issue is' : 'issues are'} still waiting to be indexed and cannot be found by meaning until they are. This is a partial answer.`,
      isComplete: false,
    }
  }

  return {
    title: 'Nothing is near enough in meaning',
    description:
      'Every issue in this workspace is indexed, and none is close enough to this text to clear the similarity floor the server applies. Try describing it differently, or search by words instead.',
    isComplete: true,
  }
}

/**
 * One sentence about what the index can currently find, shown at all times.
 *
 * Not only on an empty result. A *non*-empty result from a half-built index is
 * just as partial as an empty one -- the ten matches shown may not be the ten
 * best -- and a caveat that appears only when the answer is empty teaches the
 * reader that a full-looking answer is complete.
 */
export function describeCoverage(state: IndexState | null): string {
  if (state === null) {
    return 'How much of this workspace is indexed is not known yet.'
  }

  if (!state.enabled) {
    return 'This deployment has no embedding model, so nothing is indexed and this search cannot return anything.'
  }

  const parts = [
    `${state.indexed} indexed`,
    state.pending > 0 ? `${state.pending} waiting` : null,
    state.failed > 0 ? `${state.failed} failed` : null,
  ].filter((part): part is string => part !== null)

  const coverage = parts.join(', ')

  if (state.pending > 0) {
    return `${coverage}. Results are incomplete while anything is waiting.`
  }

  if (state.failed > 0) {
    return `${coverage}. A failed issue has stopped being retried and will not be found here.`
  }

  return `${coverage}. Every issue in this workspace can be found by meaning.`
}

/**
 * Similarity as a percentage, to the whole number.
 *
 * A cosine similarity between 0 and 1, from `EmbeddingRepository.search_similar`,
 * above a floor the server sets and a client may not lower. Rendered as a
 * percentage because that is the only form of it a reader can compare two of;
 * rounded rather than truncated, and never dressed up as a confidence -- the
 * label beside it says "similarity" and not "match".
 *
 * Values outside 0..1 are clamped, because a percentage over 100 would read as
 * a bug in the display rather than in whatever produced it.
 */
export function similarityPercent(similarity: number): number {
  return Math.round(Math.min(Math.max(similarity, 0), 1) * 100)
}
