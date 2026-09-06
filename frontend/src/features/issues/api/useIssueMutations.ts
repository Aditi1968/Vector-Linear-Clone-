import { useCallback } from 'react'
import { useMutation } from '@apollo/client/react'

import { useWorkspaceSlug } from '../../../app/routes'
import { describeError } from '../lib/errors'
import { removeArchivedIssue } from './cache'
import {
  IssueArchiveDocument,
  IssueSetCycleDocument,
  IssueSetProjectDocument,
  IssueUpdateDocument,
} from './documents'
import { failureOutcome, interpretPayload } from './outcome'
import type { IssueSaveOutcome } from './outcome'
import type { IssueDetailFields, IssuePatch } from './types'

/** Nothing interpretable came back from an archive. */
const ARCHIVE_FAILED = 'The issue could not be archived. Please try again.'

/**
 * Apply a patch to an issue as the server is about to.
 *
 * Only for the optimistic response. Each field is copied across
 * individually rather than by spreading the patch, and that is not
 * ceremony: a patch's fields are all optional, so `{ ...issue, ...patch }`
 * would widen every one of them to include `undefined` and would write
 * `title: undefined` into the cache for any field the patch left alone.
 *
 * `!= null` where the schema's own field is non-null (a title, a priority),
 * `!== undefined` where null is a meaningful value the user can choose --
 * unassigning, clearing an estimate, removing a due date. That difference is
 * the whole reason this is written out.
 */
function applyPatch(issue: IssueDetailFields, patch: IssuePatch): IssueDetailFields {
  return {
    ...issue,
    ...(patch.title != null && { title: patch.title }),
    ...(patch.description !== undefined && { description: patch.description }),
    ...(patch.priority != null && { priority: patch.priority }),
    ...(patch.workflowStateId != null && {
      workflowStateId: patch.workflowStateId,
    }),
    ...(patch.assigneeId !== undefined && { assigneeId: patch.assigneeId }),
    ...(patch.estimate !== undefined && { estimate: patch.estimate }),
    ...(patch.dueDate !== undefined && { dueDate: patch.dueDate }),
  }
}

export interface UseIssueMutationsResult {
  /** Edit any of the fields `IssueUpdateInput` accepts. */
  updateIssue: (
    issue: IssueDetailFields,
    patch: IssuePatch,
  ) => Promise<IssueSaveOutcome>
  /** Move an issue into a project, or out of every project with `null`. */
  setProject: (issueId: string, projectId: string | null) => Promise<IssueSaveOutcome>
  /** Move an issue into a cycle, or out of every cycle with `null`. */
  setCycle: (issueId: string, cycleId: string | null) => Promise<IssueSaveOutcome>
  /**
   * Take an issue off the board, removing it from the cached list.
   *
   * Resolves to a message to show, or `null` when it worked. Not an
   * `IssueSaveOutcome`, and not for want of symmetry: archiving names no
   * field a control could own -- the payload selects only an id -- so a
   * per-field channel would have nowhere to put what it carried.
   */
  archiveIssue: (issueId: string) => Promise<string | null>
  /** Any of the four is in flight. */
  isSaving: boolean
}

/**
 * Every write an open issue can make.
 *
 * ## Why the list stays in step without a single cache write
 *
 * `issueUpdate`, `issueSetProject` and `issueSetCycle` all return the issue,
 * and all three select `IssueDetailFields` -- a superset of what the list
 * selects. Apollo normalises the result over `Issue:<uuid>`, which is the
 * same entity the cached list points at, so the row re-renders with the new
 * value as a consequence of the mutation landing. There is nothing to write
 * and nothing to keep in step, and that is a property of the *documents*
 * rather than of this file: it is why ./operations.graphql builds the detail
 * fragment out of the row fragment instead of listing the fields twice.
 *
 * `issueArchive` is the exception, and the only mutation here with an
 * `update`. An archived issue is absent from every subsequent query, so
 * there is no entity to normalise -- the row has to be removed, which is
 * ./cache.ts's job.
 *
 * ## Optimism, and where it stops
 *
 * `issueUpdate` sends an optimistic response: picking a priority or a state
 * from a menu should land immediately, and every field it touches is one the
 * server echoes back unchanged when it accepts. If it does not accept,
 * Apollo drops the optimistic layer when the real result arrives and the
 * previous value returns -- a rejection needs no rollback code here.
 *
 * The placement mutations are deliberately *not* optimistic. Their result
 * carries nested `project` and `cycle` objects, so guessing it means
 * synthesising entities this hook does not have; the honest version is to
 * wait for the server, and the wait is one request.
 */
export function useIssueMutations(): UseIssueMutationsResult {
  const workspaceSlug = useWorkspaceSlug()

  const [update, updateState] = useMutation(IssueUpdateDocument)
  const [setProjectMutation, projectState] = useMutation(IssueSetProjectDocument)
  const [setCycleMutation, cycleState] = useMutation(IssueSetCycleDocument)
  const [archive, archiveState] = useMutation(IssueArchiveDocument, {
    update(cache, result) {
      const archived = result.data?.issueArchive.issue

      // Null when the server refused. There is no row to remove, and
      // `errors` is the panel's business, not the cache's.
      if (archived == null) {
        return
      }

      removeArchivedIssue(cache, archived.id, workspaceSlug)
    },
  })

  const updateIssue = useCallback(
    async (issue: IssueDetailFields, patch: IssuePatch): Promise<IssueSaveOutcome> => {
      try {
        const result = await update({
          variables: { id: issue.id, input: { ...patch, workspaceSlug } },
          optimisticResponse: {
            issueUpdate: {
              __typename: 'IssueUpdatePayload',
              issue: applyPatch(issue, patch),
              errors: [],
            },
          },
        })

        return interpretPayload(result.data?.issueUpdate)
      } catch (reason) {
        return failureOutcome(reason)
      }
    },
    [update, workspaceSlug],
  )

  const setProject = useCallback(
    async (issueId: string, projectId: string | null): Promise<IssueSaveOutcome> => {
      try {
        const result = await setProjectMutation({
          // `milestoneId` is left absent rather than sent as null: this
          // product exposes no milestone picker, so naming the field would be
          // this client asserting something about a value it never shows.
          variables: { input: { workspaceSlug, issueId, projectId } },
        })

        return interpretPayload(result.data?.issueSetProject)
      } catch (reason) {
        return failureOutcome(reason)
      }
    },
    [setProjectMutation, workspaceSlug],
  )

  const setCycle = useCallback(
    async (issueId: string, cycleId: string | null): Promise<IssueSaveOutcome> => {
      try {
        const result = await setCycleMutation({
          variables: { input: { workspaceSlug, issueId, cycleId } },
        })

        return interpretPayload(result.data?.issueSetCycle)
      } catch (reason) {
        return failureOutcome(reason)
      }
    },
    [setCycleMutation, workspaceSlug],
  )

  const archiveIssue = useCallback(
    async (issueId: string): Promise<string | null> => {
      try {
        const result = await archive({ variables: { workspaceSlug, id: issueId } })
        const payload = result.data?.issueArchive

        if (payload === undefined || (payload.issue === null && payload.errors.length === 0)) {
          return ARCHIVE_FAILED
        }

        // Joined rather than shown one at a time: these name `id`, not
        // anything on screen, so there is no control to hang them off.
        return payload.errors.length > 0
          ? payload.errors.map((error) => error.message).join(' ')
          : null
      } catch (reason) {
        return describeError(reason)
      }
    },
    [archive, workspaceSlug],
  )

  return {
    updateIssue,
    setProject,
    setCycle,
    archiveIssue,
    isSaving:
      updateState.loading ||
      projectState.loading ||
      cycleState.loading ||
      archiveState.loading,
  }
}
