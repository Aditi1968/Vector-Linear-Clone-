/**
 * The names this feature knows the server contract by.
 *
 * Aliases of generated types, every one. Nothing here declares a field, a
 * scalar or a nullability, so nothing here can disagree with the schema.
 */

import type {
  FavoriteAddMutation,
  FavoriteListQuery,
  FavoriteSavedViewsQuery,
} from '../../../generated/operations'

/**
 * One shortcut in the viewer's own list.
 *
 * `{ id, teamId, projectId, savedViewId, position }` and exactly one of the
 * three target columns is set. It carries NO name and no resolved target --
 * the schema exposes no `Favorite.team`, `Favorite.project` or
 * `Favorite.savedView` -- so a row's label is resolved from the id against
 * lists the screen already holds. ../lib/favorites.ts is where that happens.
 */
export type Favorite = FavoriteListQuery['favorites'][number]

/** One saved view, as this feature needs it: an id and a name. */
export type FavoriteSavedView = FavoriteSavedViewsQuery['savedViews']['nodes'][number]

/**
 * What a favorite can point at.
 *
 * Three typed columns rather than a polymorphic `(type, id)` pair, which is
 * the schema's own shape and is why this is a tagged union here: a call site
 * cannot supply two targets or none.
 */
export type FavoriteTarget =
  | { kind: 'team'; id: string }
  | { kind: 'project'; id: string }
  | { kind: 'savedView'; id: string }

/**
 * One entry of a payload's `errors`.
 *
 * A favorite the caller may not touch is refused with `code: "NOT_FOUND"` --
 * the same answer a nonexistent id gets, deliberately.
 */
export type FavoriteValidationError = FavoriteAddMutation['favoriteAdd']['errors'][number]
