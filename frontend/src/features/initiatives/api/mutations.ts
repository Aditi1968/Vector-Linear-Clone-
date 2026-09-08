import { useCallback } from 'react'
import { useMutation } from '@apollo/client/react'

import { useWorkspaceSlug } from '../../../app/routes'
import { readPayload } from '../../../lib/graphql'
import type { PayloadOutcome } from '../../../lib/graphql'
import { describeError } from '../../issues/lib/errors'
import {
  InitiativeClearParentDocument,
  InitiativeCreateDocument,
  InitiativeDeleteDocument,
  InitiativeProjectAddDocument,
  InitiativeProjectRemoveDocument,
  InitiativeSetParentDocument,
  InitiativeUpdateDocument,
  InitiativeUpdatePostDocument,
} from './documents'
import type {
  Health,
  InitiativeDetail,
  InitiativeDraft,
  InitiativeUpdateEntry,
  InitiativeValidationError,
} from './types'

/**
 * What an initiative write can do, as three cases that cannot be confused.
 *
 * See `src/lib/graphql/payload.ts` for the reader and for why `rejected` and
 * `failed` are not one case.
 */
export type InitiativeOutcome<T> = PayloadOutcome<T, InitiativeValidationError>

/**
 * An edit, as a patch.
 *
 * Every key optional, because `InitiativeUpdateInput` leaves a field alone
 * when it is absent. That is the opposite of `IssueTemplateUpdateInput`,
 * which is a whole-row replace, and the difference is why the editor here can
 * send four fields where the template editor has to send eleven.
 */
export type InitiativePatch = Partial<InitiativeDraft>

export interface UseInitiativeActionsResult {
  createInitiative: (draft: InitiativeDraft) => Promise<InitiativeOutcome<InitiativeDetail>>
  updateInitiative: (
    id: string,
    patch: InitiativePatch,
  ) => Promise<InitiativeOutcome<InitiativeDetail>>
  deleteInitiative: (id: string) => Promise<InitiativeOutcome<string>>
  addProject: (
    initiativeId: string,
    projectId: string,
  ) => Promise<InitiativeOutcome<InitiativeDetail>>
  removeProject: (
    initiativeId: string,
    projectId: string,
  ) => Promise<InitiativeOutcome<InitiativeDetail>>
  /** Nest this initiative under another. */
  setParent: (
    initiativeId: string,
    parentInitiativeId: string,
  ) => Promise<InitiativeOutcome<InitiativeDetail>>
  /** Lift it back to the top level. */
  clearParent: (initiativeId: string) => Promise<InitiativeOutcome<InitiativeDetail>>
  /** Report where the initiative stands, and why. */
  postUpdate: (
    initiativeId: string,
    health: Health,
    body: string,
  ) => Promise<InitiativeOutcome<InitiativeUpdateEntry>>
  isSaving: boolean
}

/**
 * Every write the initiatives screen makes.
 *
 * ## Which of these needs cache help, and which does not
 *
 * `initiativeUpdate`, `initiativeProjectAdd` and `initiativeProjectRemove`
 * each return the whole detail fragment over the same `Initiative:<id>`
 * entity the list holds, and each changes only that entity. Normalisation
 * corrects the row; nothing is refetched.
 *
 * Create and delete change the membership of the `initiatives` connection,
 * which neither payload is part of, so both refetch the list.
 *
 * Re-parenting refetches too, and for a subtler reason: the write changes
 * `parentInitiativeId` on the child -- which is returned -- and
 * `childInitiativeIds` on the old parent and the new one, which are not.
 * Without the refetch the tree would draw the moved initiative in its new
 * place and still under its old one.
 *
 * `initiativeUpdatePost` returns an `InitiativeUpdate` and nothing else,
 * while changing two things this screen renders: the initiative's `updates`
 * list, and its `health` column, which migration 022 keeps denormalised onto
 * the row so that a list can show health without reading every update. Both
 * the detail and the list are refetched for that reason.
 */
export function useInitiativeActions(): UseInitiativeActionsResult {
  const workspaceSlug = useWorkspaceSlug()

  const listOnly = ['InitiativeList']
  const listAndDetail = ['InitiativeList', 'InitiativeDetail']

  const [create, createState] = useMutation(InitiativeCreateDocument, {
    refetchQueries: listOnly,
  })
  const [update, updateState] = useMutation(InitiativeUpdateDocument)
  const [remove, removeState] = useMutation(InitiativeDeleteDocument, {
    refetchQueries: listOnly,
  })
  const [projectAdd, projectAddState] = useMutation(InitiativeProjectAddDocument)
  const [projectRemove, projectRemoveState] = useMutation(
    InitiativeProjectRemoveDocument,
  )
  const [setParentMutation, setParentState] = useMutation(InitiativeSetParentDocument, {
    refetchQueries: listOnly,
  })
  const [clearParentMutation, clearParentState] = useMutation(
    InitiativeClearParentDocument,
    { refetchQueries: listOnly },
  )
  const [postUpdateMutation, postUpdateState] = useMutation(
    InitiativeUpdatePostDocument,
    { refetchQueries: listAndDetail },
  )

  const createInitiative = useCallback(
    async (draft: InitiativeDraft) => {
      try {
        const result = await create({ variables: { input: { workspaceSlug, ...draft } } })

        return readPayload(
          result.data?.initiativeCreate,
          result.data?.initiativeCreate.initiative,
        )
      } catch (reason) {
        // The default `errorPolicy` of `none` makes `mutate` reject on a
        // top-level GraphQL error as well as on a transport failure.
        return { status: 'failed' as const, message: describeError(reason) }
      }
    },
    [create, workspaceSlug],
  )

  const updateInitiative = useCallback(
    async (id: string, patch: InitiativePatch) => {
      try {
        const result = await update({
          variables: { input: { workspaceSlug, id, ...patch } },
        })

        return readPayload(
          result.data?.initiativeUpdate,
          result.data?.initiativeUpdate.initiative,
        )
      } catch (reason) {
        return { status: 'failed' as const, message: describeError(reason) }
      }
    },
    [update, workspaceSlug],
  )

  const deleteInitiative = useCallback(
    async (id: string) => {
      try {
        const result = await remove({ variables: { input: { workspaceSlug, id } } })
        const payload = result.data?.initiativeDelete

        return readPayload(payload, payload?.deletedInitiativeId)
      } catch (reason) {
        return { status: 'failed' as const, message: describeError(reason) }
      }
    },
    [remove, workspaceSlug],
  )

  const addProject = useCallback(
    async (initiativeId: string, projectId: string) => {
      try {
        const result = await projectAdd({
          variables: { input: { workspaceSlug, initiativeId, projectId } },
        })

        return readPayload(
          result.data?.initiativeProjectAdd,
          result.data?.initiativeProjectAdd.initiative,
        )
      } catch (reason) {
        return { status: 'failed' as const, message: describeError(reason) }
      }
    },
    [projectAdd, workspaceSlug],
  )

  const removeProject = useCallback(
    async (initiativeId: string, projectId: string) => {
      try {
        const result = await projectRemove({
          variables: { input: { workspaceSlug, initiativeId, projectId } },
        })

        return readPayload(
          result.data?.initiativeProjectRemove,
          result.data?.initiativeProjectRemove.initiative,
        )
      } catch (reason) {
        return { status: 'failed' as const, message: describeError(reason) }
      }
    },
    [projectRemove, workspaceSlug],
  )

  const setParent = useCallback(
    async (initiativeId: string, parentInitiativeId: string) => {
      try {
        const result = await setParentMutation({
          variables: { input: { workspaceSlug, initiativeId, parentInitiativeId } },
        })

        return readPayload(
          result.data?.initiativeSetParent,
          result.data?.initiativeSetParent.initiative,
        )
      } catch (reason) {
        return { status: 'failed' as const, message: describeError(reason) }
      }
    },
    [setParentMutation, workspaceSlug],
  )

  const clearParent = useCallback(
    async (initiativeId: string) => {
      try {
        const result = await clearParentMutation({
          variables: { input: { workspaceSlug, initiativeId } },
        })

        return readPayload(
          result.data?.initiativeClearParent,
          result.data?.initiativeClearParent.initiative,
        )
      } catch (reason) {
        return { status: 'failed' as const, message: describeError(reason) }
      }
    },
    [clearParentMutation, workspaceSlug],
  )

  const postUpdate = useCallback(
    async (initiativeId: string, health: Health, body: string) => {
      try {
        const result = await postUpdateMutation({
          variables: { input: { workspaceSlug, initiativeId, health, body } },
        })

        return readPayload(
          result.data?.initiativeUpdatePost,
          result.data?.initiativeUpdatePost.update,
        )
      } catch (reason) {
        return { status: 'failed' as const, message: describeError(reason) }
      }
    },
    [postUpdateMutation, workspaceSlug],
  )

  return {
    createInitiative,
    updateInitiative,
    deleteInitiative,
    addProject,
    removeProject,
    setParent,
    clearParent,
    postUpdate,
    isSaving:
      createState.loading ||
      updateState.loading ||
      removeState.loading ||
      projectAddState.loading ||
      projectRemoveState.loading ||
      setParentState.loading ||
      clearParentState.loading ||
      postUpdateState.loading,
  }
}
