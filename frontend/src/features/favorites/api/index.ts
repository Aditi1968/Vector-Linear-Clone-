/**
 * The favorites data adapter.
 *
 * The boundary the rest of the feature is written against. The screen imports
 * hooks and types from here and never a document or an Apollo hook. The
 * documents are exported too, and only from this module, because mocking a
 * response requires the exact document that produced it.
 */

export { useFavoriteList, useFavoriteSavedViews } from './queries'
export type { UseFavoriteListResult } from './queries'

export { useFavoriteActions } from './mutations'
export type { FavoriteOutcome, UseFavoriteActionsResult } from './mutations'

export {
  FavoriteAddDocument,
  FavoriteListDocument,
  FavoriteRemoveDocument,
  FavoriteReorderDocument,
  FavoriteSavedViewsDocument,
} from './documents'

export type {
  Favorite,
  FavoriteSavedView,
  FavoriteTarget,
  FavoriteValidationError,
} from './types'
