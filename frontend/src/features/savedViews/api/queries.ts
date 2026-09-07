import { useCallback } from 'react'
import { useQuery } from '@apollo/client/react'

import { useWorkspaceSlug } from '../../../app/routes'
import { describeError } from '../../issues/lib/errors'
import { SavedViewListDocument, SavedViewResultsDocument } from './documents'
import type { SavedViewFields, SavedViewIssue } from './types'

/** See ../../../features/cycles/api/queries.ts for why an id is checked before it is sent. */
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

const NO_VIEWS: readonly SavedViewFields[] = []
const NO_ISSUES: readonly SavedViewIssue[] = []

export interface UseSavedViewListResult {
  views: readonly SavedViewFields[]
  /** The server has more views than this page. Reported, not offered. */
  hasNextPage: boolean
  isLoading: boolean
  errorMessage: string | null
  retry: () => void
}

/**
 * Every saved view the viewer can see.
 *
 * One page of 25. There is no "Load more" for the same reason the triage
 * queue has none: `savedViews` has no field policy in
 * `src/lib/graphql/cache.ts`, so `fetchMore` would write page two under a
 * cache key no mounted query watches and the button would silently do
 * nothing. Unlike `triageIssues`, this connection's nodes *are* normalised
 * entities, so the existing `cursorConnectionPolicy(['workspaceSlug'])` would
 * work here -- but that is an edit to a shared file, and it is reported
 * rather than made in passing.
 *
 * A workspace with more than 25 saved views is not the common case, and the
 * screen says so when it happens rather than pretending the list is whole.
 */
export function useSavedViewList(): UseSavedViewListResult {
  const workspaceSlug = useWorkspaceSlug()

  const { data, error, loading, refetch } = useQuery(SavedViewListDocument, {
    variables: { workspaceSlug, after: null },
  })

  const retry = useCallback(() => {
    // Swallowed: `refetch` rejects *and* sets `error` on the hook result, and
    // `error` is what the screen renders.
    void refetch().catch(() => undefined)
  }, [refetch])

  return {
    views: data?.savedViews.nodes ?? NO_VIEWS,
    hasNextPage: data?.savedViews.pageInfo.hasNextPage ?? false,
    isLoading: loading,
    errorMessage: error === undefined ? null : describeError(error),
    retry,
  }
}

export interface UseSavedViewResultsResult {
  /** The issues the view selects, as far as one page goes. */
  issues: readonly SavedViewIssue[]
  /** How many the view selects altogether, paging ignored. */
  totalCount: number
  hasNextPage: boolean
  isLoading: boolean
  /** The server answered "no such view". A successful response, not an error. */
  isNotFound: boolean
  errorMessage: string | null
  retry: () => void
}

/**
 * What one saved view actually selects.
 *
 * `SavedView.issues` is the only way to ask this. The filter and the ordering
 * come from the stored row rather than from the document, so the page that
 * comes back is the one that was saved -- there is no way to preview an
 * unsaved filter, and this feature does not pretend otherwise.
 *
 * Skipped until there is a view to ask about, and skipped for an id that
 * cannot be a UUID: a `UUID!` coerced from `''` is a top-level GraphQL error,
 * which would report "nothing selected yet" as "something went wrong".
 */
export function useSavedViewResults(
  viewId: string | null,
): UseSavedViewResultsResult {
  const workspaceSlug = useWorkspaceSlug()
  const isRequestable = viewId !== null && UUID_PATTERN.test(viewId)

  const { data, error, loading, refetch } = useQuery(SavedViewResultsDocument, {
    variables: { workspaceSlug, id: viewId ?? '', after: null },
    skip: !isRequestable,
  })

  const retry = useCallback(() => {
    void refetch().catch(() => undefined)
  }, [refetch])

  const connection = data?.savedView?.issues

  return {
    issues: connection?.nodes ?? NO_ISSUES,
    totalCount: connection?.totalCount ?? 0,
    hasNextPage: connection?.pageInfo.hasNextPage ?? false,
    isLoading: isRequestable && loading,
    // A view that answered null is gone, or was never the viewer's to see --
    // the server gives one answer to both on purpose.
    isNotFound: data !== undefined && data.savedView === null,
    errorMessage: error === undefined ? null : describeError(error),
    retry,
  }
}
