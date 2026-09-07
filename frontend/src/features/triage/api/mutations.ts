import { useCallback } from 'react'
import { useMutation } from '@apollo/client/react'

import { useWorkspaceSlug } from '../../../app/routes'
import { describeError } from '../../issues/lib/errors'
import {
  TriageAcceptDocument,
  TriageChangeTeamDocument,
  TriageDeclineDocument,
  TriageIssueUpdateDocument,
  TriageMarkDuplicateDocument,
} from './documents'
import type { TriageValidationError } from './types'

const UNEXPECTED_RESPONSE = 'That did not save. Please try again.'

/**
 * What a triage write can do, as three cases that cannot be confused.
 *
 * `rejected` is the payload's `errors`: expected input the server refused,
 * arriving inside `data` over a 200 with a `field` naming what to fix --
 * accepting into a state that is not that team's, marking an issue a
 * duplicate of itself. `failed` is a rejected promise, which no field owns.
 */
export type TriageOutcome =
  | { status: 'ok' }
  | { status: 'rejected'; errors: readonly TriageValidationError[] }
  | { status: 'failed'; message: string }

/**
 * Read one payload the same way every time.
 *
 * ponytail: a small generic that four features in this wave each hold a copy
 * of, beside the three copies of `describeError` that
 * `features/screens.tsx` already documents. Folding them into `src/lib`
 * is the right fix and belongs to whoever owns that directory; four copies
 * that agree beat one import reaching across a feature boundary for a
 * stranger's helper.
 */
function readPayload(
  payload: { issue: { id: string } | null; errors: readonly TriageValidationError[] } | undefined,
): TriageOutcome {
  if (payload === undefined) {
    return { status: 'failed', message: UNEXPECTED_RESPONSE }
  }

  // Checked first: the backend's contract is that exactly one of the two is
  // populated, and the errors are the more specific answer.
  if (payload.errors.length > 0) {
    return { status: 'rejected', errors: payload.errors }
  }

  if (payload.issue === null) {
    return { status: 'failed', message: UNEXPECTED_RESPONSE }
  }

  return { status: 'ok' }
}

export interface UseTriageActionsResult {
  /** Accept an issue onto the board, in the state the caller chose. */
  accept: (issueId: string, workflowStateId: string) => Promise<TriageOutcome>
  /** Refuse the issue. It leaves the queue without reaching the board. */
  decline: (issueId: string) => Promise<TriageOutcome>
  /** Close the issue as a duplicate of another. */
  markDuplicate: (issueId: string, duplicateOfId: string) => Promise<TriageOutcome>
  /** Hand the issue to another team's queue. */
  changeTeam: (issueId: string, teamId: string) => Promise<TriageOutcome>
  /** Set priority or assignee without leaving the queue. */
  updateIssue: (
    issueId: string,
    patch: { priority?: number; assigneeId?: string | null },
  ) => Promise<TriageOutcome>
  isSaving: boolean
}

/**
 * Every write the triage screen makes.
 *
 * ## Why every one of them refetches
 *
 * Each of these changes which rows are in the queue, and membership of a
 * server-filtered connection is not a property of any entity in it -- so
 * normalising the returned `Issue` cannot move a row out of a cached list.
 * Accepting, declining and marking a duplicate all remove the row; changing
 * team moves it to another team's queue entirely.
 *
 * `updateIssue` refetches for a subtler reason. It writes through
 * `issueUpdate`, whose payload normalises over `Issue:<id>` -- but the row
 * above it came from `TriageIssue.issue`, which is an `IssueSummary`, a
 * different cache type under a different key. `IssueSummary:<id>` is not
 * `Issue:<id>`, so the new priority would land in the store and never reach
 * the row. That is a cache-identity fact, not an oversight, and it is why a
 * priority set from this screen is followed by a refetch.
 *
 * `refetchQueries` by operation name reruns whichever `TriageQueue` is
 * mounted with the variables it was mounted with, which is the team the
 * picker is on.
 */
export function useTriageActions(): UseTriageActionsResult {
  const workspaceSlug = useWorkspaceSlug()

  const refetchQueries = ['TriageQueue']

  const [acceptMutation, acceptState] = useMutation(TriageAcceptDocument, { refetchQueries })
  const [declineMutation, declineState] = useMutation(TriageDeclineDocument, { refetchQueries })
  const [duplicateMutation, duplicateState] = useMutation(TriageMarkDuplicateDocument, {
    refetchQueries,
  })
  const [changeTeamMutation, changeTeamState] = useMutation(TriageChangeTeamDocument, {
    refetchQueries,
  })
  const [updateMutation, updateState] = useMutation(TriageIssueUpdateDocument, {
    refetchQueries,
  })

  const accept = useCallback(
    async (issueId: string, workflowStateId: string) => {
      try {
        const result = await acceptMutation({
          variables: { input: { workspaceSlug, issueId, workflowStateId } },
        })

        return readPayload(result.data?.triageAccept)
      } catch (reason) {
        // The default `errorPolicy` of `none` makes `mutate` reject on a
        // top-level GraphQL error as well as on a transport failure, so this
        // is the only place either can arrive. Returned rather than
        // re-thrown: a failed write is an outcome the screen renders.
        return { status: 'failed' as const, message: describeError(reason) }
      }
    },
    [acceptMutation, workspaceSlug],
  )

  const decline = useCallback(
    async (issueId: string) => {
      try {
        const result = await declineMutation({
          variables: { input: { workspaceSlug, issueId } },
        })

        return readPayload(result.data?.triageDecline)
      } catch (reason) {
        return { status: 'failed' as const, message: describeError(reason) }
      }
    },
    [declineMutation, workspaceSlug],
  )

  const markDuplicate = useCallback(
    async (issueId: string, duplicateOfId: string) => {
      try {
        const result = await duplicateMutation({
          variables: { input: { workspaceSlug, issueId, duplicateOfId } },
        })

        return readPayload(result.data?.triageMarkDuplicate)
      } catch (reason) {
        return { status: 'failed' as const, message: describeError(reason) }
      }
    },
    [duplicateMutation, workspaceSlug],
  )

  const changeTeam = useCallback(
    async (issueId: string, teamId: string) => {
      try {
        const result = await changeTeamMutation({
          variables: { input: { workspaceSlug, issueId, teamId } },
        })

        return readPayload(result.data?.triageChangeTeam)
      } catch (reason) {
        return { status: 'failed' as const, message: describeError(reason) }
      }
    },
    [changeTeamMutation, workspaceSlug],
  )

  const updateIssue = useCallback(
    async (issueId: string, patch: { priority?: number; assigneeId?: string | null }) => {
      try {
        // Only the keys the caller supplied are sent. `IssueUpdateInput`
        // treats every field past `workspaceSlug` as optional, so this is a
        // patch: an omitted `assigneeId` leaves the assignee alone, where an
        // explicit `null` clears it. Spreading `patch` rather than listing
        // both fields is what keeps those two cases distinct.
        const result = await updateMutation({
          variables: { id: issueId, input: { workspaceSlug, ...patch } },
        })

        return readPayload(result.data?.issueUpdate)
      } catch (reason) {
        return { status: 'failed' as const, message: describeError(reason) }
      }
    },
    [updateMutation, workspaceSlug],
  )

  return {
    accept,
    decline,
    markDuplicate,
    changeTeam,
    updateIssue,
    isSaving:
      acceptState.loading ||
      declineState.loading ||
      duplicateState.loading ||
      changeTeamState.loading ||
      updateState.loading,
  }
}
