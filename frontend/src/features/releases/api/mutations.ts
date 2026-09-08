import { useCallback } from 'react'
import { useMutation } from '@apollo/client/react'

import { useWorkspaceSlug } from '../../../app/routes'
import { readPayload } from '../../../lib/graphql'
import type { PayloadOutcome } from '../../../lib/graphql'
import { describeError } from '../../issues/lib/errors'
import {
  ReleaseCreateDocument,
  ReleaseDeleteDocument,
  ReleaseStatusSetDocument,
} from './documents'
import type {
  ReleaseDetail,
  ReleaseDraft,
  ReleaseStatus,
  ReleaseValidationError,
} from './types'

/**
 * What a release write can do, as three cases that cannot be confused.
 *
 * See `src/lib/graphql/payload.ts` for the reader and for why `rejected` and
 * `failed` are not one case.
 */
export type ReleaseOutcome<T> = PayloadOutcome<T, ReleaseValidationError>

export interface UseReleaseActionsResult {
  createRelease: (draft: ReleaseDraft) => Promise<ReleaseOutcome<ReleaseDetail>>
  /**
   * Move a release to its next status.
   *
   * Which moves are legal is `RELEASE_TRANSITIONS` in
   * `app/domain/releases.py`, mirrored in ../lib/releases.ts so a menu can
   * offer only the legal ones. The server is still the authority; it applies
   * the move in one statement that names the states it is willing to move
   * FROM, so a concurrent transition loses rather than being overwritten, and
   * the loser is told.
   */
  setStatus: (
    id: string,
    status: ReleaseStatus,
  ) => Promise<ReleaseOutcome<ReleaseDetail>>
  deleteRelease: (id: string) => Promise<ReleaseOutcome<string>>
  isSaving: boolean
}

/**
 * Every write the releases screen makes.
 *
 * ## Which of these needs cache help, and which does not
 *
 * `releaseStatusSet` returns the whole detail fragment over the same
 * `Release:<id>` entity the list holds, and changes only that entity's
 * `status` and `deployedAt`. Normalisation corrects the row; nothing is
 * refetched.
 *
 * Create and delete change the membership of the `releases` connection, which
 * neither payload is part of, so both refetch the list.
 *
 * ## There is no edit
 *
 * `releaseCreate`, `releaseStatusSet` and `releaseDelete` are the whole of
 * the schema's release mutations. A release's name, commit range and notes
 * cannot be changed after it is cut, and that is migration 024's central
 * decision rather than a missing endpoint: "the shape this file exists to
 * make impossible is a release note that changes after it was published."
 * The screen says so rather than offering an Edit that would fail.
 */
export function useReleaseActions(): UseReleaseActionsResult {
  const workspaceSlug = useWorkspaceSlug()

  const listOnly = ['ReleaseList']

  const [create, createState] = useMutation(ReleaseCreateDocument, {
    refetchQueries: listOnly,
  })
  const [statusSet, statusSetState] = useMutation(ReleaseStatusSetDocument)
  const [remove, removeState] = useMutation(ReleaseDeleteDocument, {
    refetchQueries: listOnly,
  })

  const createRelease = useCallback(
    async (draft: ReleaseDraft) => {
      try {
        const result = await create({ variables: { input: { workspaceSlug, ...draft } } })

        return readPayload(
          result.data?.releaseCreate,
          result.data?.releaseCreate.release,
        )
      } catch (reason) {
        // The default `errorPolicy` of `none` makes `mutate` reject on a
        // top-level GraphQL error as well as on a transport failure.
        return { status: 'failed' as const, message: describeError(reason) }
      }
    },
    [create, workspaceSlug],
  )

  const setStatus = useCallback(
    async (id: string, status: ReleaseStatus) => {
      try {
        const result = await statusSet({
          variables: { input: { workspaceSlug, id, status } },
        })

        return readPayload(
          result.data?.releaseStatusSet,
          result.data?.releaseStatusSet.release,
        )
      } catch (reason) {
        return { status: 'failed' as const, message: describeError(reason) }
      }
    },
    [statusSet, workspaceSlug],
  )

  const deleteRelease = useCallback(
    async (id: string) => {
      try {
        const result = await remove({ variables: { input: { workspaceSlug, id } } })
        const payload = result.data?.releaseDelete

        return readPayload(payload, payload?.deletedReleaseId)
      } catch (reason) {
        return { status: 'failed' as const, message: describeError(reason) }
      }
    },
    [remove, workspaceSlug],
  )

  return {
    createRelease,
    setStatus,
    deleteRelease,
    isSaving: createState.loading || statusSetState.loading || removeState.loading,
  }
}
