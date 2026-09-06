import { useLazyQuery } from '@apollo/client/react'
import { useEffect, useState } from 'react'

import { describeError } from '../../issues/lib/errors'
import { IssueSearchDocument } from './documents'
import type { IssueSearchHit } from './types'

const NO_HITS: readonly IssueSearchHit[] = []

/** Below this, a search is not a search -- it is every issue in the workspace. */
const MIN_QUERY_LENGTH = 2

/** How long typing has to stop before a request goes out. */
const DEBOUNCE_MS = 250

export interface UseIssueSearchResult {
  hits: readonly IssueSearchHit[]
  isSearching: boolean
  errorMessage: string | null
  /** True once a query has been typed but nothing came back. */
  isEmpty: boolean
}

/**
 * Find an issue by name, for the relation and sub-issue pickers.
 *
 * `search` and not `issues`: choosing one issue out of a workspace's worth is
 * not something a paged list does well, and the server already has an index
 * for exactly this.
 *
 * Debounced rather than fired per keystroke. The complexity budget is not the
 * reason -- this document costs 60 of 1000 -- the reason is that a request
 * per character puts eight searches in flight for a four-letter word and
 * leaves the last-arriving one on screen, which is how a picker ends up
 * showing results for a prefix the user has already finished typing.
 * Apollo's `useLazyQuery` does the ordering; the timer keeps the count down.
 */
export function useIssueSearch(
  workspaceSlug: string,
  query: string,
): UseIssueSearchResult {
  const trimmed = query.trim()
  const isRequestable = trimmed.length >= MIN_QUERY_LENGTH

  const [run, { data, loading, error }] = useLazyQuery(IssueSearchDocument)
  const [debounced, setDebounced] = useState('')

  useEffect(() => {
    if (!isRequestable) {
      setDebounced('')
      return
    }

    const timer = setTimeout(() => {
      setDebounced(trimmed)
    }, DEBOUNCE_MS)

    return () => {
      clearTimeout(timer)
    }
  }, [isRequestable, trimmed])

  useEffect(() => {
    if (debounced === '') {
      return
    }

    // Rejections are read off the hook's own `error`, so the promise's is a
    // duplicate that would otherwise surface as an unhandled rejection.
    void run({ variables: { workspaceSlug, query: debounced } }).catch(
      () => undefined,
    )
  }, [debounced, run, workspaceSlug])

  const hits = isRequestable ? (data?.search.issues ?? NO_HITS) : NO_HITS

  return {
    hits,
    isSearching: isRequestable && (loading || debounced !== trimmed),
    errorMessage: error === undefined ? null : describeError(error),
    isEmpty: isRequestable && !loading && debounced === trimmed && hits.length === 0,
  }
}

/** Exported so a picker can say what it is waiting for rather than guessing. */
export { MIN_QUERY_LENGTH }
