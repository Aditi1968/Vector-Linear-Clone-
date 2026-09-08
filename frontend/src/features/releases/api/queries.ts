import { useCallback, useState } from 'react'
import { NetworkStatus } from '@apollo/client'
import { useQuery } from '@apollo/client/react'

import { useWorkspaceSlug } from '../../../app/routes'
import { describeError } from '../../issues/lib/errors'
import { ReleaseDetailDocument, ReleaseListDocument } from './documents'
import type { Release, ReleaseDetail } from './types'

/**
 * Canonical hyphenated UUID.
 *
 * Checked before the request rather than after it: a malformed id fails at
 * *variable coercion*, which is a top-level GraphQL error, and that would put
 * a "something went wrong" panel in front of somebody whose actual situation
 * is that they have selected nothing yet.
 */
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

/** Stable identities for "nothing yet", so memoised children are not defeated. */
const NO_RELEASES: readonly Release[] = []

export interface UseReleaseListResult {
  releases: readonly Release[]
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
 * The release list, paginated by cursor, newest first.
 *
 * Pages accumulate in the *cache*, not here: `src/lib/graphql/cache.ts` owns
 * the `releases` field policy that merges them, and it is the only thing that
 * does. A hook that also concatenated pages would double every row the moment
 * both ran.
 *
 * Read by two screens -- the releases list and the environments screen, which
 * uses it to say what was last deployed where. Both mount the same document
 * with the same variables, so the second is answered from the cache.
 */
export function useReleaseList(): UseReleaseListResult {
  const workspaceSlug = useWorkspaceSlug()
  const [loadMoreErrorMessage, setLoadMoreErrorMessage] = useState<string | null>(null)

  const { data, error, networkStatus, fetchMore, refetch } = useQuery(
    ReleaseListDocument,
    {
      // `after: null` stated rather than omitted: the merge policy reads
      // `args.after` to decide whether a result starts the list or extends
      // it, and the decision should read a value the document declares.
      variables: { workspaceSlug, after: null },
      notifyOnNetworkStatusChange: true,
    },
  )

  const connection = data?.releases
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
    releases: connection?.nodes ?? NO_RELEASES,
    hasNextPage,
    isLoadingFirstPage: connection === undefined && error === undefined,
    isLoadingMore,
    errorMessage: error === undefined ? null : describeError(error),
    loadMoreErrorMessage,
    loadMore,
    retry,
  }
}

export interface UseReleaseDetailResult {
  release: ReleaseDetail | null
  isLoading: boolean
  /** The server answered "no such release". A successful response. */
  isNotFound: boolean
  errorMessage: string | null
  retry: () => void
}

/**
 * One release, with its frozen note and the issues it shipped.
 *
 * Skipped until something is selected. A `UUID!` coerced from `''` is a
 * top-level GraphQL error, which would report "nothing selected yet" as
 * "something went wrong".
 */
export function useReleaseDetail(releaseId: string | null): UseReleaseDetailResult {
  const workspaceSlug = useWorkspaceSlug()
  const isRequestable = releaseId !== null && UUID_PATTERN.test(releaseId)

  const { data, error, loading, refetch } = useQuery(ReleaseDetailDocument, {
    variables: { workspaceSlug, id: releaseId ?? '' },
    skip: !isRequestable,
  })

  const retry = useCallback(() => {
    void refetch().catch(() => undefined)
  }, [refetch])

  return {
    release: data?.release ?? null,
    isLoading: isRequestable && loading,
    // Null is gone, or was never the viewer's to see -- the server gives one
    // answer to both on purpose, and there is no error code that would
    // separate them.
    isNotFound: data !== undefined && data.release === null,
    errorMessage: error === undefined ? null : describeError(error),
    retry,
  }
}
