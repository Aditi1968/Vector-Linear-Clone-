import { useCallback, useRef, useState } from 'react'
import { NetworkStatus } from '@apollo/client'
import { useQuery } from '@apollo/client/react'

import { useWorkspaceSlug } from '../../../app/routes'
import type { IssueRowFields } from '../../issues/api'
import { describeError } from '../lib/errors'
import { BoardIssuesDocument } from './documents'

/** Stable identity for "no cards", so a render with no data does not churn memos. */
const NO_ISSUES: readonly IssueRowFields[] = []

export interface UseBoardIssuesResult {
  /** Every card loaded so far. Accumulated by the cache's field policy, not here. */
  issues: readonly IssueRowFields[]
  /** Whether the team has more issues than have been paged in. */
  hasNextPage: boolean
  isLoadingFirstPage: boolean
  isLoadingMore: boolean
  /** A failure that concerns the board as a whole. */
  errorMessage: string | null
  /** A failure of the most recent "load more", which leaves loaded cards intact. */
  loadMoreErrorMessage: string | null
  loadMore: () => void
  retry: () => void
}

/**
 * One team's issues, by cursor.
 *
 * Deliberately close to `features/issues/api/useIssueList.ts` and not a
 * generalisation of it: the two differ in exactly one argument (`teamId`) and
 * the shared machinery -- page accumulation, the `after: null` reset, the
 * dedupe -- already lives in one place, the cache's field policy in
 * `src/lib/graphql/cache.ts`. Nothing here concatenates pages, and a hook that
 * did would double every card the moment a second page arrived.
 *
 * ## The team may not be known yet
 *
 * `teamId` is `UUID!`, so there is no "all teams" value to send. Until the
 * workspace context has answered with the teams there is no question to ask,
 * and the query is skipped rather than sent with a placeholder -- an empty
 * string coerced into a `UUID!` is a top-level GraphQL error, which would
 * report "still loading" to the user as "something went wrong".
 */
export function useBoardIssues(teamId: string | undefined): UseBoardIssuesResult {
  const workspaceSlug = useWorkspaceSlug()
  const [loadMoreErrorMessage, setLoadMoreErrorMessage] = useState<string | null>(null)

  /**
   * The cursor currently being fetched.
   *
   * Stops a second `fetchMore` for the same cursor from being sent at all --
   * a double click, an effect invoked twice in development. The merge policy
   * would dedupe the result anyway; this makes the intent explicit. A ref
   * because it must be read and written within one event and must never
   * cause a render.
   */
  const inFlightCursor = useRef<string | null>(null)

  const { data, error, networkStatus, fetchMore, refetch } = useQuery(
    BoardIssuesDocument,
    {
      // `after: null` rather than omitted: the merge policy reads `args.after`
      // to decide whether a result starts the list or extends it.
      variables: { workspaceSlug, teamId: teamId ?? '', after: null },
      skip: teamId === undefined,
      notifyOnNetworkStatusChange: true,
    },
  )

  const connection = data?.issues
  const issues = connection?.nodes ?? NO_ISSUES
  const endCursor = connection?.pageInfo.endCursor ?? null
  const hasNextPage = connection?.pageInfo.hasNextPage ?? false

  const loadMore = useCallback(() => {
    // Nothing to ask for. Asking anyway would send `after: null`, which the
    // merge policy reads as "start the list over" and which would discard
    // every page already loaded.
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
        // clear the cards already on screen, and an unhandled rejection would
        // be reported as a page-level crash by any error tracker.
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
    // Swallowed: `refetch` rejects *and* sets `error` on the hook result, and
    // `error` is what the screen renders.
    void refetch().catch(() => undefined)
  }, [refetch])

  return {
    issues,
    hasNextPage,
    isLoadingFirstPage: networkStatus === NetworkStatus.loading,
    isLoadingMore: networkStatus === NetworkStatus.fetchMore,
    errorMessage: error === undefined ? null : describeError(error),
    loadMoreErrorMessage,
    loadMore,
    retry,
  }
}
