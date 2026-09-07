import { useCallback } from 'react'
import { useMutation } from '@apollo/client/react'

import { useWorkspaceSlug } from '../../../app/routes'
import { describeError } from '../../issues/lib/errors'
import {
  FavoriteAddDocument,
  FavoriteRemoveDocument,
  FavoriteReorderDocument,
} from './documents'
import type { FavoriteTarget, FavoriteValidationError } from './types'

const UNEXPECTED_RESPONSE = 'That did not save. Please try again.'

/** What a favorites write can do, as three cases that cannot be confused. */
export type FavoriteOutcome =
  | { status: 'ok' }
  | { status: 'rejected'; errors: readonly FavoriteValidationError[] }
  | { status: 'failed'; message: string }

/**
 * Read one payload the same way every time.
 *
 * ponytail: see the note in `features/triage/api/mutations.ts` -- this is one
 * of four copies in this wave, awaiting a home in `src/lib`.
 */
function readPayload(
  payload: { errors: readonly FavoriteValidationError[] } | undefined,
  value: unknown,
): FavoriteOutcome {
  if (payload === undefined) {
    return { status: 'failed', message: UNEXPECTED_RESPONSE }
  }

  // Checked first: the backend's contract is that exactly one of the two is
  // populated, and the errors are the more specific answer.
  if (payload.errors.length > 0) {
    return { status: 'rejected', errors: payload.errors }
  }

  if (value === null || value === undefined) {
    return { status: 'failed', message: UNEXPECTED_RESPONSE }
  }

  return { status: 'ok' }
}

export interface UseFavoriteActionsResult {
  /** Star a team, a project or a saved view. */
  addFavorite: (target: FavoriteTarget) => Promise<FavoriteOutcome>
  removeFavorite: (id: string) => Promise<FavoriteOutcome>
  /**
   * Apply a whole reorder plan, in order.
   *
   * Takes the plan rather than a direction because `favoriteReorder` moves
   * one row per call and a move may touch several -- see
   * ../lib/favorites.ts. Stops at the first failure and reports it: a
   * half-applied plan is still a valid list (positions need be neither unique
   * nor contiguous), so there is nothing to roll back, and continuing past a
   * refusal would only pile up more of them.
   */
  reorderFavorites: (
    plan: readonly { id: string; position: number }[],
  ) => Promise<FavoriteOutcome>
  isSaving: boolean
}

/**
 * Every write the favorites screen makes.
 *
 * All three refetch `FavoriteList`. Adding and removing change the membership
 * of a plain list field that no payload contains, and reordering changes the
 * `position` of rows other than the one it returns -- the server renumbers
 * nothing, but a plan moves several, and the list's *order* is what the
 * screen renders. Guessing the neighbours' new positions client-side is how
 * two tabs end up disagreeing about an order only the server decides.
 */
export function useFavoriteActions(): UseFavoriteActionsResult {
  const workspaceSlug = useWorkspaceSlug()

  const refetchQueries = ['FavoriteList']

  const [add, addState] = useMutation(FavoriteAddDocument, { refetchQueries })
  const [remove, removeState] = useMutation(FavoriteRemoveDocument, { refetchQueries })
  const [reorder, reorderState] = useMutation(FavoriteReorderDocument, { refetchQueries })

  const addFavorite = useCallback(
    async (target: FavoriteTarget) => {
      try {
        /*
          Exactly one column set, and the other two explicitly null.
          `FavoriteAddInput` refuses none and refuses more than one rather
          than guessing, so expanding a tagged union here is what makes it
          impossible for a call site to send an invalid combination.
        */
        const result = await add({
          variables: {
            input: {
              workspaceSlug,
              teamId: target.kind === 'team' ? target.id : null,
              projectId: target.kind === 'project' ? target.id : null,
              savedViewId: target.kind === 'savedView' ? target.id : null,
            },
          },
        })

        return readPayload(result.data?.favoriteAdd, result.data?.favoriteAdd.favorite)
      } catch (reason) {
        // The default `errorPolicy` of `none` makes `mutate` reject on a
        // top-level GraphQL error as well as on a transport failure.
        return { status: 'failed' as const, message: describeError(reason) }
      }
    },
    [add, workspaceSlug],
  )

  const removeFavorite = useCallback(
    async (id: string) => {
      try {
        const result = await remove({ variables: { input: { workspaceSlug, id } } })
        const payload = result.data?.favoriteRemove

        return readPayload(payload, payload?.deletedFavoriteId)
      } catch (reason) {
        return { status: 'failed' as const, message: describeError(reason) }
      }
    },
    [remove, workspaceSlug],
  )

  const reorderFavorites = useCallback(
    async (plan: readonly { id: string; position: number }[]) => {
      try {
        for (const entry of plan) {
          // Sequential and not `Promise.all`: these writes are to one
          // ordering, and firing them together makes the final order depend
          // on which round trip finishes last.
          const result = await reorder({
            variables: { input: { workspaceSlug, id: entry.id, position: entry.position } },
          })

          const outcome = readPayload(
            result.data?.favoriteReorder,
            result.data?.favoriteReorder.favorite,
          )

          if (outcome.status !== 'ok') {
            return outcome
          }
        }

        return { status: 'ok' as const }
      } catch (reason) {
        return { status: 'failed' as const, message: describeError(reason) }
      }
    },
    [reorder, workspaceSlug],
  )

  return {
    addFavorite,
    removeFavorite,
    reorderFavorites,
    isSaving: addState.loading || removeState.loading || reorderState.loading,
  }
}
