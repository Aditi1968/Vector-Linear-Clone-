import { useCallback, useRef, useState } from 'react'
import { NetworkStatus } from '@apollo/client'
import { useQuery } from '@apollo/client/react'

import { useWorkspaceSlug } from '../../../app/routes'
import { describeError } from '../lib/errors'
import { IssueListDocument } from './documents'
import type { IssueRowFields } from './types'

/**
 * Stable identity for "no rows", so a render that has no data does not hand
 * the list a fresh array and defeat every memoised child below it.
 */
const NO_ISSUES: readonly IssueRowFields[] = []

export interface UseIssueListResult {
  /** Every row loaded so far, newest first. Accumulated by the cache, not here. */
  issues: readonly IssueRowFields[]
  /** Whether the connection has more rows after the last one loaded. */
  hasNextPage: boolean
  /** The very first fetch, with nothing on screen yet. */
  isLoadingFirstPage: boolean
  /** A `fetchMore`, with rows already on screen. Deliberately separate. */
  isLoadingMore: boolean
  /** A retry of the whole list. */
  isRefreshing: boolean
  /** A failure that concerns the list as a whole. */
  errorMessage: string | null
  /** A failure of the most recent "load more", which leaves loaded rows intact. */
  loadMoreErrorMessage: string | null
  loadMore: () => void
  retry: () => void
}

/**
 * The issue list, paginated by cursor.
 *
 * ## Pages accumulate in the cache, not in this hook
 *
 * There is no `useState<Issue[]>` here and no `[...previous, ...next]`
 * anywhere in this feature. `src/lib/graphql/cache.ts` owns a field policy
 * that merges pages of `issues`, and it is the only thing that does. A hook
 * that also concatenated pages would double every row the moment both ran --
 * which is the usual way "load more shows duplicates" gets built, and it is
 * invisible until there is a second page to load.
 *
 * So `fetchMore` is called for its side effect on the cache; its resolved
 * value is deliberately ignored. What comes back out of `data` is the merged
 * list.
 *
 * ## Three loading states, not one boolean
 *
 * `networkStatus` distinguishes them and `loading` does not -- `loading` is
 * true for all three. The distinction is what lets the screen show a skeleton
 * on first load, keep every row on screen during "load more", and say
 * something different again while retrying. `notifyOnNetworkStatusChange` is
 * set explicitly: it defaults to true in Apollo Client v4, but this hook's
 * entire loading model depends on the status changing mid-flight, and a
 * default that quietly flipped would turn "load more" into a dead button
 * rather than into a type error.
 *
 * ## The workspace comes from the URL
 *
 * `issues` requires a `workspaceSlug`, and this hook reads it from the route
 * rather than taking it as an argument. Two reasons, and neither is
 * convenience: no screen that renders this list has a workspace to pass that
 * the URL does not already state, and a prop would let two components on one
 * page disagree about which tenant they are showing. The cache keys pages on
 * the same argument (see `src/lib/graphql/cache.ts`), so moving between
 * workspaces reads a different list rather than merging two.
 */
export function useIssueList(): UseIssueListResult {
  const workspaceSlug = useWorkspaceSlug()
  const [loadMoreErrorMessage, setLoadMoreErrorMessage] = useState<string | null>(null)

  /**
   * The cursor currently being fetched, if any.
   *
   * A guard against dispatching the same `fetchMore` twice -- a double click
   * landing inside one React batch, an effect invoked twice in development.
   * The cache's merge policy would dedupe the result anyway; this stops the
   * second request from being sent at all, and makes the intent explicit
   * rather than leaving it to be inferred from the policy.
   *
   * A ref and not state: it must be readable and writable synchronously
   * within one event, and it must never cause a render.
   */
  const inFlightCursor = useRef<string | null>(null)

  const { data, error, networkStatus, fetchMore, refetch } = useQuery(IssueListDocument, {
    // `after: null` rather than omitted. The merge policy reads `args.after`
    // to decide whether a result starts the list or extends it, and stating
    // the cursor keeps that decision reading from a value the document
    // declares.
    variables: { workspaceSlug, after: null },
    notifyOnNetworkStatusChange: true,
  })

  const connection = data?.issues
  const issues = connection?.nodes ?? NO_ISSUES
  const endCursor = connection?.pageInfo.endCursor ?? null
  const hasNextPage = connection?.pageInfo.hasNextPage ?? false

  const isLoadingMore = networkStatus === NetworkStatus.fetchMore

  const loadMore = useCallback(() => {
    // `hasNextPage` false is the end of the connection, and a null cursor on
    // a non-empty page would mean the server had no position to resume from.
    // Either way there is nothing to ask for, and asking anyway would send
    // `after: null`, which the merge policy reads as "start the list over"
    // and which would wipe every page already loaded.
    if (!hasNextPage || endCursor === null) {
      return
    }

    if (inFlightCursor.current === endCursor) {
      return
    }

    inFlightCursor.current = endCursor
    setLoadMoreErrorMessage(null)

    void fetchMore({ variables: { after: endCursor } })
      .catch((reason: unknown) => {
        // Caught rather than left to reject: a failed "load more" must not
        // clear the rows already on screen, and an unhandled rejection here
        // would be reported as a page-level crash by any error tracker.
        setLoadMoreErrorMessage(describeError(reason))
      })
      .finally(() => {
        if (inFlightCursor.current === endCursor) {
          inFlightCursor.current = null
        }
      })
  }, [endCursor, fetchMore, hasNextPage])

  const retry = useCallback(() => {
    setLoadMoreErrorMessage(null)

    // The rejection is swallowed on purpose: `refetch` rejects *and* sets
    // `error` on the hook result, and `error` is what the screen renders.
    // Re-reporting the same failure as an unhandled rejection adds noise and
    // no information.
    void refetch().catch(() => undefined)
  }, [refetch])

  return {
    issues,
    hasNextPage,
    isLoadingFirstPage: networkStatus === NetworkStatus.loading,
    isLoadingMore,
    isRefreshing: networkStatus === NetworkStatus.refetch,
    errorMessage: error === undefined ? null : describeError(error),
    loadMoreErrorMessage,
    loadMore,
    retry,
  }
}
