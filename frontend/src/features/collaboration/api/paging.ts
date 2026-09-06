import { useCallback, useRef, useState } from 'react'
import { NetworkStatus } from '@apollo/client'

import { describeError } from '../../issues/lib/errors'
import type { ConnectionPageInfo } from './types'

export interface Paging {
  /** Whether the connection has more rows after the last one loaded. */
  hasNextPage: boolean
  /** A `fetchMore`, with rows already on screen. Never the first load. */
  isLoadingMore: boolean
  /** A failure of the most recent "load more". Loaded rows stay put. */
  loadMoreErrorMessage: string | null
  loadMore: () => void
}

/**
 * "Load more" for one cursor-paginated panel.
 *
 * Three panels here page a connection and all three want the same behaviour,
 * so it is written once rather than three times with two of them drifting.
 *
 * ## Pages accumulate in the cache, not in a hook
 *
 * There is no `useState<T[]>` and no `[...previous, ...next]` anywhere in
 * this feature. `src/lib/graphql/cache.ts` owns the merge policy for every
 * connection and is the only thing that concatenates pages. A hook that also
 * concatenated would double every row the moment both ran -- which is the
 * usual way "load more shows duplicates" gets built, and it is invisible
 * until there is a second page.
 *
 * So `fetchMore` is called for its side effect on the cache and its resolved
 * value is ignored; what comes back out of `data` is the merged connection.
 */
export function useLoadMore(
  pageInfo: ConnectionPageInfo | undefined,
  networkStatus: NetworkStatus,
  /**
   * `fetchMore` with the cursor filled in. A one-argument callback rather
   * than Apollo's own function so this hook stays free of the query's
   * variables type -- each caller passes a closure the compiler has already
   * checked against its own document.
   */
  fetchAfter: (after: string) => Promise<unknown>,
): Paging {
  const [loadMoreErrorMessage, setLoadMoreErrorMessage] = useState<string | null>(null)

  /**
   * The cursor currently being fetched, if any.
   *
   * A guard against dispatching the same `fetchMore` twice -- a double click
   * inside one React batch, an effect invoked twice in development. The merge
   * policy would dedupe the result anyway; this stops the second request from
   * being sent at all.
   *
   * A ref and not state: it must be readable and writable synchronously
   * within one event, and it must never cause a render.
   */
  const inFlightCursor = useRef<string | null>(null)

  /**
   * The caller's closure, held so that `loadMore` does not change identity on
   * every render. A new `fetchAfter` is created each render by every caller;
   * putting it in the dependency array would make `loadMore` unstable and
   * defeat any memoisation below it.
   */
  const fetchRef = useRef(fetchAfter)
  fetchRef.current = fetchAfter

  const hasNextPage = pageInfo?.hasNextPage ?? false
  const endCursor = pageInfo?.endCursor ?? null

  const loadMore = useCallback(() => {
    // `hasNextPage` false is the end of the connection, and a null cursor on
    // a non-empty page would mean the server had no position to resume from.
    // Either way there is nothing to ask for, and asking anyway would send
    // `after: null`, which the merge policy reads as "start the list over"
    // and which would discard every page already loaded.
    if (!hasNextPage || endCursor === null || inFlightCursor.current === endCursor) {
      return
    }

    inFlightCursor.current = endCursor
    setLoadMoreErrorMessage(null)

    void fetchRef
      .current(endCursor)
      .catch((reason: unknown) => {
        // Caught rather than left to reject: a failed "load more" must not
        // clear the rows already on screen, and an unhandled rejection would
        // be reported as a page-level crash by any error tracker.
        setLoadMoreErrorMessage(describeError(reason))
      })
      .finally(() => {
        if (inFlightCursor.current === endCursor) {
          inFlightCursor.current = null
        }
      })
  }, [endCursor, hasNextPage])

  return {
    hasNextPage,
    isLoadingMore: networkStatus === NetworkStatus.fetchMore,
    loadMoreErrorMessage,
    loadMore,
  }
}
