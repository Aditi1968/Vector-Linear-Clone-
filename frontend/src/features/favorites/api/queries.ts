import { useCallback } from 'react'
import { useQuery } from '@apollo/client/react'

import { useWorkspaceSlug } from '../../../app/routes'
import { describeError } from '../../issues/lib/errors'
import { FavoriteListDocument, FavoriteSavedViewsDocument } from './documents'
import type { Favorite, FavoriteSavedView } from './types'

const NO_FAVORITES: readonly Favorite[] = []
const NO_VIEWS: readonly FavoriteSavedView[] = []

export interface UseFavoriteListResult {
  favorites: readonly Favorite[]
  isLoading: boolean
  errorMessage: string | null
  retry: () => void
}

/**
 * The viewer's own shortcut list for this workspace.
 *
 * Not paginated, because `favorites` takes no page-size argument: it is a
 * sidebar list, and the server returns all of it in `position, id` order.
 * Per-user and per-workspace -- the same person in two workspaces has two
 * independent lists.
 */
export function useFavoriteList(): UseFavoriteListResult {
  const workspaceSlug = useWorkspaceSlug()

  const { data, error, loading, refetch } = useQuery(FavoriteListDocument, {
    variables: { workspaceSlug },
  })

  const retry = useCallback(() => {
    // Swallowed: `refetch` rejects *and* sets `error` on the hook result, and
    // `error` is what the screen renders.
    void refetch().catch(() => undefined)
  }, [refetch])

  return {
    favorites: data?.favorites ?? NO_FAVORITES,
    isLoading: loading,
    errorMessage: error === undefined ? null : describeError(error),
    retry,
  }
}

/**
 * The saved views a favorite can name, and the ones it can point at.
 *
 * A second document rather than a field on `Favorite`: the schema has no
 * `Favorite.savedView`, so the name behind `savedViewId` has to be looked up.
 * One request for the screen, read from a map -- not one per row.
 *
 * One page of 50. A favorite naming a view past it resolves to no name, and
 * ../components says so rather than showing a blank.
 */
export function useFavoriteSavedViews(): readonly FavoriteSavedView[] {
  const workspaceSlug = useWorkspaceSlug()

  const { data } = useQuery(FavoriteSavedViewsDocument, {
    variables: { workspaceSlug },
  })

  return data?.savedViews.nodes ?? NO_VIEWS
}
