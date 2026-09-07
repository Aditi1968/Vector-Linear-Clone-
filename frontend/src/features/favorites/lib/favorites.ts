/**
 * Turning a favorite's id into something a person can read.
 *
 * Pure functions, no React and no Apollo, so the join and the reordering
 * arithmetic are testable without a rendered screen.
 */

import type { Favorite, FavoriteTarget } from '../api/types'

/** What kind of thing a favorite points at, and its id. */
export function targetOf(favorite: Favorite): FavoriteTarget | null {
  if (favorite.teamId !== null) {
    return { kind: 'team', id: favorite.teamId }
  }

  if (favorite.projectId !== null) {
    return { kind: 'project', id: favorite.projectId }
  }

  if (favorite.savedViewId !== null) {
    return { kind: 'savedView', id: favorite.savedViewId }
  }

  /*
    The schema says exactly one of the three is set, and the service enforces
    it on the way in. This branch is what happens if that ever stops being
    true -- a fourth target column added server-side, say. Returning null
    lets the row say "this shortcut points at something this version does not
    understand" instead of rendering a blank line or throwing.
  */
  return null
}

/** A favorite resolved against the lists the screen holds. */
export interface ResolvedFavorite {
  favorite: Favorite
  target: FavoriteTarget | null
  /** What to call it, or null when the target could not be found. */
  name: string | null
  /** "Team", "Project", "Saved view" -- what kind of shortcut this is. */
  kindLabel: string
}

const KIND_LABELS: Record<FavoriteTarget['kind'], string> = {
  team: 'Team',
  project: 'Project',
  savedView: 'Saved view',
}

export interface FavoriteLookups {
  teamName: (id: string) => string | undefined
  projectName: (id: string) => string | undefined
  savedViewName: (id: string) => string | undefined
}

/**
 * Resolve one favorite's label.
 *
 * `name` is null when the target is not in the lists the screen loaded, and
 * that is deliberately not the same as an error. Two ordinary things produce
 * it: the target was deleted and the favorite outlived it, or it is a saved
 * view past the first page of `FavoriteSavedViews`. The row states which case
 * it cannot tell apart rather than inventing a name.
 */
export function resolveFavorite(
  favorite: Favorite,
  lookups: FavoriteLookups,
): ResolvedFavorite {
  const target = targetOf(favorite)

  if (target === null) {
    return { favorite, target: null, name: null, kindLabel: 'Shortcut' }
  }

  const name =
    target.kind === 'team'
      ? lookups.teamName(target.id)
      : target.kind === 'project'
        ? lookups.projectName(target.id)
        : lookups.savedViewName(target.id)

  return {
    favorite,
    target,
    name: name ?? null,
    kindLabel: KIND_LABELS[target.kind],
  }
}

/**
 * The list in the order the server sorts it: `ORDER BY position, id`.
 *
 * Sorted here as well as there because a reorder refetch and a locally
 * reasoned move have to agree about what "the row above" means. `position`
 * is neither unique nor contiguous, so the id tiebreak is not optional -- two
 * favorites sharing a position would otherwise order arbitrarily and "move
 * up" would swap a different pair each render.
 */
export function inServerOrder<T extends Favorite>(favorites: readonly T[]): T[] {
  return [...favorites].sort(
    (left, right) =>
      left.position - right.position || left.id.localeCompare(right.id),
  )
}

/**
 * The `favoriteReorder` calls that move one favorite by one place.
 *
 * ## Why this renumbers rather than swapping two positions
 *
 * `favoriteReorder` sets one row's position and the server does not
 * renumber: positions are "neither unique nor contiguous" and ties break by
 * id. So swapping two positions is only unambiguous when they already differ,
 * and on a list where several rows share a position -- which the schema
 * explicitly permits -- a swap can leave the order unchanged and the button
 * looking broken.
 *
 * Assigning every row its index makes the list contiguous and distinct, after
 * which every later move really is a two-call swap. Only rows whose position
 * actually changes are sent, so the first move on a tidy list costs two
 * requests and not `n`.
 *
 * ponytail: O(n) requests the first time an untidy list is touched. A bulk
 * `favoriteReorderAll` would make it one, and is the upgrade if a favorites
 * list ever gets long enough for that to matter.
 *
 * Returns an empty array when the move is not possible -- the top row moving
 * up, the bottom row moving down -- so the caller sends nothing rather than
 * a no-op request.
 */
export function reorderPlan(
  favorites: readonly Favorite[],
  id: string,
  direction: 'up' | 'down',
): { id: string; position: number }[] {
  const ordered = inServerOrder(favorites)
  const index = ordered.findIndex((favorite) => favorite.id === id)

  if (index === -1) {
    return []
  }

  const target = direction === 'up' ? index - 1 : index + 1

  if (target < 0 || target >= ordered.length) {
    return []
  }

  const moved = [...ordered]
  const [item] = moved.splice(index, 1)

  if (item === undefined) {
    return []
  }

  moved.splice(target, 0, item)

  const currentPosition = new Map(
    favorites.map((favorite) => [favorite.id, favorite.position]),
  )

  // `POSITION_MIN` is 0 (`app/services/saved_views.py`), so an index is
  // itself a valid position and no offset is needed.
  //
  // Only rows whose position actually changes are sent. On a list that is
  // already 0..n-1 that is exactly the two rows being swapped.
  return moved
    .map((favorite, position) => ({ id: favorite.id, position }))
    .filter((entry) => currentPosition.get(entry.id) !== entry.position)
}
