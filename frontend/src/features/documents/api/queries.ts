import { useCallback, useState } from 'react'
import { NetworkStatus } from '@apollo/client'
import { useQuery } from '@apollo/client/react'

import { useWorkspaceSlug } from '../../../app/routes'
import { describeError } from '../../issues/lib/errors'
import { DocumentDetailDocument, DocumentListDocument } from './documents'
import type { DocumentDetail, DocumentRow } from './types'

/** See ../../projects/api/queries.ts for why an id is checked before it is sent. */
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

const NO_DOCUMENTS: readonly DocumentRow[] = []

export interface UseDocumentListResult {
  documents: readonly DocumentRow[]
  hasNextPage: boolean
  isLoadingFirstPage: boolean
  isLoadingMore: boolean
  errorMessage: string | null
  loadMoreErrorMessage: string | null
  loadMore: () => void
  retry: () => void
}

/**
 * Every document in the workspace, paginated by cursor.
 *
 * Pages accumulate in the *cache*: `src/lib/graphql/cache.ts` owns the
 * `documents` field policy, whose key list includes `projectId` and
 * `initiativeId` because narrowing by either names a genuinely different
 * list rather than a page of this one.
 *
 * Neither narrowing is sent here. A document attached to no project and no
 * initiative is a workspace document -- the commonest kind -- and filtering
 * by either would hide exactly those.
 */
export function useDocumentList(): UseDocumentListResult {
  const workspaceSlug = useWorkspaceSlug()
  const [loadMoreErrorMessage, setLoadMoreErrorMessage] = useState<string | null>(null)

  const { data, error, networkStatus, fetchMore, refetch } = useQuery(
    DocumentListDocument,
    {
      variables: { workspaceSlug, after: null },
      notifyOnNetworkStatusChange: true,
    },
  )

  const connection = data?.documents
  const endCursor = connection?.pageInfo.endCursor ?? null
  const hasNextPage = connection?.pageInfo.hasNextPage ?? false
  const isLoadingMore = networkStatus === NetworkStatus.fetchMore

  const loadMore = useCallback(() => {
    // `after: null` would be read by the merge policy as "start the list
    // over" and would wipe every page already loaded.
    if (!hasNextPage || endCursor === null || isLoadingMore) {
      return
    }

    setLoadMoreErrorMessage(null)

    void fetchMore({ variables: { after: endCursor } }).catch((reason: unknown) => {
      setLoadMoreErrorMessage(describeError(reason))
    })
  }, [endCursor, fetchMore, hasNextPage, isLoadingMore])

  const retry = useCallback(() => {
    setLoadMoreErrorMessage(null)
    void refetch().catch(() => undefined)
  }, [refetch])

  return {
    documents: connection?.nodes ?? NO_DOCUMENTS,
    hasNextPage,
    isLoadingFirstPage: connection === undefined && error === undefined,
    isLoadingMore,
    errorMessage: error === undefined ? null : describeError(error),
    loadMoreErrorMessage,
    loadMore,
    retry,
  }
}

export interface UseDocumentDetailResult {
  document: DocumentDetail | null
  isLoading: boolean
  /** The server answered "no such document". A successful response. */
  isNotFound: boolean
  errorMessage: string | null
  retry: () => void
}

/**
 * One document, with its five most recent versions and its discussion.
 *
 * Skipped until something is selected, and for an id that cannot be a UUID: a
 * `UUID!` coerced from `''` is a top-level GraphQL error, which would report
 * "nothing open yet" as "something went wrong".
 */
export function useDocumentDetail(documentId: string | null): UseDocumentDetailResult {
  const workspaceSlug = useWorkspaceSlug()
  const isRequestable = documentId !== null && UUID_PATTERN.test(documentId)

  const { data, error, loading, refetch } = useQuery(DocumentDetailDocument, {
    variables: { workspaceSlug, id: documentId ?? '' },
    skip: !isRequestable,
  })

  const retry = useCallback(() => {
    void refetch().catch(() => undefined)
  }, [refetch])

  return {
    document: data?.document ?? null,
    isLoading: isRequestable && loading,
    isNotFound: data !== undefined && data.document === null,
    errorMessage: error === undefined ? null : describeError(error),
    retry,
  }
}
