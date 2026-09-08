import { useCallback, useState } from 'react'
import { NetworkStatus } from '@apollo/client'
import { useQuery } from '@apollo/client/react'

import { useWorkspaceSlug } from '../../../app/routes'
import { describeError } from '../../issues/lib/errors'
import { InitiativeDetailDocument, InitiativeListDocument } from './documents'
import type { Initiative, InitiativeDetail } from './types'

/**
 * Canonical hyphenated UUID.
 *
 * Checked before the request rather than after it, for the reason
 * `features/projects/api/queries.ts` gives: a malformed id fails at *variable
 * coercion*, which is a top-level GraphQL error, and that would put a
 * "something went wrong" panel in front of somebody whose actual situation is
 * that they selected nothing yet.
 */
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

/** Stable identities for "nothing yet", so memoised children are not defeated. */
const NO_INITIATIVES: readonly Initiative[] = []

export interface UseInitiativeListResult {
  initiatives: readonly Initiative[]
  hasNextPage: boolean
  /** The very first fetch, with nothing on screen. */
  isLoadingFirstPage: boolean
  /** A `fetchMore`, with rows already on screen. Deliberately separate. */
  isLoadingMore: boolean
  errorMessage: string | null
  loadMoreErrorMessage: string | null
  loadMore: () => void
  retry: () => void
}

/**
 * The initiative list, paginated by cursor.
 *
 * Pages accumulate in the *cache*, not here: `src/lib/graphql/cache.ts` owns
 * the `initiatives` field policy that merges them, and it is the only thing
 * that does. A hook that also concatenated pages would double every row the
 * moment both ran.
 *
 * Loading more matters more on this screen than on most, because the tree the
 * page draws is built from `parentInitiativeId` across the rows in hand: a
 * child whose parent is on page two cannot be drawn under it until page two
 * arrives. The page says so rather than quietly showing a flat list -- see
 * ../lib/initiatives.ts.
 */
export function useInitiativeList(): UseInitiativeListResult {
  const workspaceSlug = useWorkspaceSlug()
  const [loadMoreErrorMessage, setLoadMoreErrorMessage] = useState<string | null>(null)

  const { data, error, networkStatus, fetchMore, refetch } = useQuery(
    InitiativeListDocument,
    {
      // `after: null` stated rather than omitted: the merge policy reads
      // `args.after` to decide whether a result starts the list or extends
      // it, and the decision should read a value the document declares.
      variables: { workspaceSlug, after: null },
      notifyOnNetworkStatusChange: true,
    },
  )

  const connection = data?.initiatives
  const endCursor = connection?.pageInfo.endCursor ?? null
  const hasNextPage = connection?.pageInfo.hasNextPage ?? false
  const isLoadingMore = networkStatus === NetworkStatus.fetchMore

  const loadMore = useCallback(() => {
    // A null cursor would mean the server had no position to resume from.
    // Asking anyway would send `after: null`, which the merge policy reads as
    // "start the list over" and which would wipe every page already loaded.
    if (!hasNextPage || endCursor === null || isLoadingMore) {
      return
    }

    setLoadMoreErrorMessage(null)

    void fetchMore({ variables: { after: endCursor } }).catch((reason: unknown) => {
      // Caught rather than left to reject: a failed "load more" must not
      // clear the rows already on screen.
      setLoadMoreErrorMessage(describeError(reason))
    })
  }, [endCursor, fetchMore, hasNextPage, isLoadingMore])

  const retry = useCallback(() => {
    setLoadMoreErrorMessage(null)
    void refetch().catch(() => undefined)
  }, [refetch])

  return {
    initiatives: connection?.nodes ?? NO_INITIATIVES,
    hasNextPage,
    isLoadingFirstPage: connection === undefined && error === undefined,
    isLoadingMore,
    errorMessage: error === undefined ? null : describeError(error),
    loadMoreErrorMessage,
    loadMore,
    retry,
  }
}

export interface UseInitiativeDetailResult {
  initiative: InitiativeDetail | null
  isLoading: boolean
  /** The server answered "no such initiative". A successful response. */
  isNotFound: boolean
  errorMessage: string | null
  retry: () => void
}

/**
 * One initiative, with the updates posted on it.
 *
 * Skipped until something is selected. A `UUID!` coerced from `''` is a
 * top-level GraphQL error, which would report "nothing selected yet" as
 * "something went wrong".
 */
export function useInitiativeDetail(
  initiativeId: string | null,
): UseInitiativeDetailResult {
  const workspaceSlug = useWorkspaceSlug()
  const isRequestable = initiativeId !== null && UUID_PATTERN.test(initiativeId)

  const { data, error, loading, refetch } = useQuery(InitiativeDetailDocument, {
    variables: { workspaceSlug, id: initiativeId ?? '' },
    skip: !isRequestable,
  })

  const retry = useCallback(() => {
    void refetch().catch(() => undefined)
  }, [refetch])

  return {
    initiative: data?.initiative ?? null,
    isLoading: isRequestable && loading,
    // Null is gone, or was never the viewer's to see -- the server gives one
    // answer to both on purpose, and there is no error code that would
    // separate them.
    isNotFound: data !== undefined && data.initiative === null,
    errorMessage: error === undefined ? null : describeError(error),
    retry,
  }
}
