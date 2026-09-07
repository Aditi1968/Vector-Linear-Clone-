import { useCallback } from 'react'
import { useMutation } from '@apollo/client/react'

import { useWorkspaceSlug } from '../../../app/routes'
import { describeError } from '../../issues/lib/errors'
import {
  SavedViewCreateDocument,
  SavedViewDeleteDocument,
  SavedViewUpdateDocument,
} from './documents'
import type { SavedViewDraft, SavedViewFields, SavedViewValidationError } from './types'

const UNEXPECTED_RESPONSE = 'That did not save. Please try again.'

/**
 * What a write can do, as three cases that cannot be confused.
 *
 * `rejected` is the payload's `errors`: expected input being refused,
 * arriving inside `data` over a 200 with a `field` naming what to fix.
 * `failed` is a rejected promise -- an outage, a bug -- which no field owns.
 */
export type SavedViewOutcome<T> =
  | { status: 'ok'; value: T }
  | { status: 'rejected'; errors: readonly SavedViewValidationError[] }
  | { status: 'failed'; message: string }

/**
 * Read one payload the same way every time.
 *
 * ponytail: the fourth copy of a small generic in this wave -- see the note
 * in `features/triage/api/mutations.ts`. Folding them into `src/lib` is the
 * right fix and belongs to whoever owns that directory.
 */
function readPayload<T>(
  payload: { errors: readonly SavedViewValidationError[] } | undefined,
  value: T | null | undefined,
): SavedViewOutcome<T> {
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

  return { status: 'ok', value }
}

/**
 * A patch for `savedViewUpdate`.
 *
 * Every key optional and `filter` optional *separately* from the rest, which
 * is the whole point: `SavedViewUpdateInput` leaves a field alone when it is
 * absent, so a caller that cannot faithfully rewrite a stored filter (see
 * ../lib/savedViews.ts) omits `filter` and still renames the view.
 */
export type SavedViewPatch = Partial<SavedViewDraft>

export interface UseSavedViewActionsResult {
  createView: (draft: SavedViewDraft) => Promise<SavedViewOutcome<SavedViewFields>>
  updateView: (
    id: string,
    patch: SavedViewPatch,
  ) => Promise<SavedViewOutcome<SavedViewFields>>
  deleteView: (id: string) => Promise<SavedViewOutcome<string>>
  isSaving: boolean
}

/**
 * Every write the saved-view screen makes.
 *
 * ## Which of these needs cache help
 *
 * `savedViewUpdate` returns the whole `SavedViewFields` fragment over the
 * same `SavedView:<id>` entity the list is holding, so the row and the open
 * results panel both correct themselves by normalisation. Nothing to do.
 *
 * Create and delete change the *membership* of `savedViews`, which
 * normalising an entity never fixes, so both refetch by operation name --
 * which reruns whichever `SavedViewList` is mounted with the variables it was
 * mounted with.
 *
 * Update also refetches `SavedViewResults`, and for a reason normalisation
 * cannot cover: changing a view's filter or ordering changes *which issues it
 * selects*, and the membership of `SavedView.issues` is a server-side answer
 * that no amount of correcting the view's own fields will recompute.
 */
export function useSavedViewActions(): UseSavedViewActionsResult {
  const workspaceSlug = useWorkspaceSlug()

  const [create, createState] = useMutation(SavedViewCreateDocument, {
    refetchQueries: ['SavedViewList'],
  })
  const [update, updateState] = useMutation(SavedViewUpdateDocument, {
    refetchQueries: ['SavedViewResults'],
  })
  const [remove, removeState] = useMutation(SavedViewDeleteDocument, {
    refetchQueries: ['SavedViewList'],
  })

  const createView = useCallback(
    async (draft: SavedViewDraft) => {
      try {
        const result = await create({ variables: { input: { ...draft, workspaceSlug } } })

        return readPayload(result.data?.savedViewCreate, result.data?.savedViewCreate.savedView)
      } catch (reason) {
        // The default `errorPolicy` of `none` makes `mutate` reject on a
        // top-level GraphQL error as well as on a transport failure, so this
        // is the only place either can arrive. Returned rather than
        // re-thrown: a failed save is an outcome the form renders.
        return { status: 'failed' as const, message: describeError(reason) }
      }
    },
    [create, workspaceSlug],
  )

  const updateView = useCallback(
    async (id: string, patch: SavedViewPatch) => {
      try {
        // Spread rather than listed: an absent key means "leave it alone" and
        // an explicit null means "clear it", and only spreading the caller's
        // own object keeps those two distinct. Listing the fields here would
        // turn every omission into a null.
        const result = await update({ variables: { input: { workspaceSlug, id, ...patch } } })

        return readPayload(result.data?.savedViewUpdate, result.data?.savedViewUpdate.savedView)
      } catch (reason) {
        return { status: 'failed' as const, message: describeError(reason) }
      }
    },
    [update, workspaceSlug],
  )

  const deleteView = useCallback(
    async (id: string) => {
      try {
        const result = await remove({ variables: { input: { workspaceSlug, id } } })
        const payload = result.data?.savedViewDelete

        return readPayload(payload, payload?.deletedSavedViewId)
      } catch (reason) {
        return { status: 'failed' as const, message: describeError(reason) }
      }
    },
    [remove, workspaceSlug],
  )

  return {
    createView,
    updateView,
    deleteView,
    isSaving: createState.loading || updateState.loading || removeState.loading,
  }
}
