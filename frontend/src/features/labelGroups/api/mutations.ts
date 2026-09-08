import { useCallback } from 'react'
import { useMutation } from '@apollo/client/react'

import { useWorkspaceSlug } from '../../../app/routes'
import { readPayload } from '../../../lib/graphql'
import type { PayloadOutcome } from '../../../lib/graphql'
import { describeError } from '../../issues/lib/errors'
import {
  LabelGroupCreateDocument,
  LabelGroupDeleteDocument,
  LabelGroupUpdateDocument,
  LabelSetGroupDocument,
} from './documents'
import type {
  GroupedLabel,
  LabelGroup,
  LabelGroupDraft,
  LabelGroupValidationError,
} from './types'

/**
 * What a label-group write can do, as three cases that cannot be confused.
 *
 * See `src/lib/graphql/payload.ts` for the reader and for why `rejected` and
 * `failed` are not one case. The distinction earns its keep here more than
 * almost anywhere: the refusal this feature exists around -- "you cannot make
 * this group exclusive while issues break the rule it would impose" -- arrives
 * as `rejected`, with a field, over an HTTP 200.
 */
export type LabelGroupOutcome<T> = PayloadOutcome<T, LabelGroupValidationError>

export interface UseLabelGroupActionsResult {
  createGroup: (draft: LabelGroupDraft) => Promise<LabelGroupOutcome<LabelGroup>>
  /**
   * Rename a group, or change whether it is exclusive.
   *
   * Both fields are always sent: `LabelGroupUpdateInput` requires both, so
   * this is a whole-row replace and not a patch.
   */
  updateGroup: (
    id: string,
    draft: LabelGroupDraft,
  ) => Promise<LabelGroupOutcome<LabelGroup>>
  deleteGroup: (id: string) => Promise<LabelGroupOutcome<string>>
  /** Put a label into a group, or -- with `null` -- take it out of one. */
  setLabelGroup: (
    labelId: string,
    groupId: string | null,
  ) => Promise<LabelGroupOutcome<GroupedLabel>>
  isSaving: boolean
}

/**
 * Every write the label-groups screen makes.
 *
 * ## Which of these needs cache help, and which does not
 *
 * `labelGroupUpdate` returns the whole group fragment over the same
 * `LabelGroup:<id>` entity the list holds, so normalisation corrects the row.
 * `labelSetGroup` does the same for `Label:<id>`, and membership lives on the
 * label -- a group carries no member list of its own -- so moving a label into
 * a group needs nothing refetched either.
 *
 * Create and delete change the membership of the `labelGroups` LIST, which
 * neither payload is part of, so both refetch it.
 *
 * Delete refetches the labels too, and that is the one non-obvious entry.
 * `LabelService` ungroups the group's labels first, in the same transaction --
 * migration 021's `labels_group_fk` is ON DELETE RESTRICT precisely so that
 * "ungroup them" and "delete them" cannot be chosen silently by a constraint.
 * Those labels' `groupId` is now null and no payload says so, so without the
 * refetch the screen would keep drawing them under a group that no longer
 * exists.
 */
export function useLabelGroupActions(): UseLabelGroupActionsResult {
  const workspaceSlug = useWorkspaceSlug()

  const groupsOnly = ['LabelGroupList']
  const groupsAndLabels = ['LabelGroupList', 'GroupedLabelList']

  const [create, createState] = useMutation(LabelGroupCreateDocument, {
    refetchQueries: groupsOnly,
  })
  const [update, updateState] = useMutation(LabelGroupUpdateDocument)
  const [remove, removeState] = useMutation(LabelGroupDeleteDocument, {
    refetchQueries: groupsAndLabels,
  })
  const [setGroup, setGroupState] = useMutation(LabelSetGroupDocument)

  const createGroup = useCallback(
    async (draft: LabelGroupDraft) => {
      try {
        const result = await create({ variables: { input: { workspaceSlug, ...draft } } })

        return readPayload(
          result.data?.labelGroupCreate,
          result.data?.labelGroupCreate.group,
        )
      } catch (reason) {
        // The default `errorPolicy` of `none` makes `mutate` reject on a
        // top-level GraphQL error as well as on a transport failure.
        return { status: 'failed' as const, message: describeError(reason) }
      }
    },
    [create, workspaceSlug],
  )

  const updateGroup = useCallback(
    async (id: string, draft: LabelGroupDraft) => {
      try {
        const result = await update({
          variables: { input: { workspaceSlug, id, ...draft } },
        })

        return readPayload(
          result.data?.labelGroupUpdate,
          result.data?.labelGroupUpdate.group,
        )
      } catch (reason) {
        return { status: 'failed' as const, message: describeError(reason) }
      }
    },
    [update, workspaceSlug],
  )

  const deleteGroup = useCallback(
    async (id: string) => {
      try {
        const result = await remove({ variables: { input: { workspaceSlug, id } } })
        const payload = result.data?.labelGroupDelete

        return readPayload(payload, payload?.deletedGroupId)
      } catch (reason) {
        return { status: 'failed' as const, message: describeError(reason) }
      }
    },
    [remove, workspaceSlug],
  )

  const setLabelGroup = useCallback(
    async (labelId: string, groupId: string | null) => {
      try {
        const result = await setGroup({
          variables: { input: { workspaceSlug, labelId, groupId } },
        })

        return readPayload(result.data?.labelSetGroup, result.data?.labelSetGroup.label)
      } catch (reason) {
        return { status: 'failed' as const, message: describeError(reason) }
      }
    },
    [setGroup, workspaceSlug],
  )

  return {
    createGroup,
    updateGroup,
    deleteGroup,
    setLabelGroup,
    isSaving:
      createState.loading ||
      updateState.loading ||
      removeState.loading ||
      setGroupState.loading,
  }
}
