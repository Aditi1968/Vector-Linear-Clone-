import { useCallback } from 'react'
import { useMutation } from '@apollo/client/react'

import { useWorkspaceSlug } from '../../../app/routes'
import type { IssueRowFields } from '../../issues/api'
import { describeError } from '../lib/errors'
import { BoardIssueMoveDocument } from './documents'

/**
 * Nothing interpretable came back. Distinct from a rejected patch and from a
 * transport failure, and rare enough that a bespoke sentence would be more
 * alarming than useful.
 */
const UNEXPECTED_RESPONSE = 'The issue could not be moved. Please try again.'

export interface UseMoveIssueResult {
  /**
   * Move a card into a workflow state.
   *
   * Resolves to a message to announce when the move failed, or `null` when it
   * worked. Not the issues feature's three-case `IssueSaveOutcome`: a rejected
   * patch here can only ever name `workflowStateId`, and the board has no
   * per-field control to hang a message off -- so the two failure channels are
   * both a sentence for the live region, and pretending otherwise would be a
   * union the caller has to destructure for no gain.
   */
  moveIssue: (issue: IssueRowFields, workflowStateId: string) => Promise<string | null>
  isMoving: boolean
}

/**
 * The board's one write.
 *
 * ## The optimistic response, and the rollback that comes free with it
 *
 * A card must land in its new column on the drop, not a round trip later. The
 * optimistic response is the card the caller already holds with one field
 * changed, which is exactly what the server echoes back when it accepts --
 * `issueUpdate` is a patch, so every other field is untouched by construction.
 *
 * That it is built from an `IssueRowFields` and not an `IssueDetailFields` is
 * what makes it honest: an optimistic response has to satisfy the document's
 * whole selection set, and ./operations.graphql selects the row fragment
 * precisely so the board never has to invent a `description` it has never
 * loaded.
 *
 * Rollback is Apollo's optimistic layer being dropped, which happens on every
 * failure path -- a rejected promise, or a payload whose `errors` are
 * non-empty and whose `issue` is null. The card returns to the column it came
 * from with no code here to put it back, and no `useState` copy of the board
 * to fall out of step. Writing the rollback by hand is how a board ends up
 * with a card in two columns.
 *
 * One field is deliberately *not* predicted: `completedAt`. The server derives
 * it from the destination state's category, so a move into a completed column
 * leaves the old value on screen for one round trip and the real result
 * corrects it. Guessing it here would mean re-implementing a server rule in
 * the browser and being wrong the day it changes.
 *
 * ## No refetch, unlike the other membership writes
 *
 * A move can take a card out of an active status filter, and correcting the
 * normalised issue does not remove it from a server-filtered connection -- so
 * the card stays on the board in its new column. That is deliberate, and the
 * opposite of the project and cycle panels, where a row that refused to leave
 * would read as a button that did nothing: here the card is visibly where it
 * was just put, which is the more useful answer. Refetching would instead
 * discard every page loaded past the first (the merge policy reads the
 * cursorless refetch as "start the list over") on every single drag.
 */
export function useMoveIssue(): UseMoveIssueResult {
  const workspaceSlug = useWorkspaceSlug()
  const [move, { loading }] = useMutation(BoardIssueMoveDocument)

  const moveIssue = useCallback(
    async (issue: IssueRowFields, workflowStateId: string): Promise<string | null> => {
      try {
        const result = await move({
          variables: { id: issue.id, input: { workspaceSlug, workflowStateId } },
          optimisticResponse: {
            issueUpdate: {
              __typename: 'IssueUpdatePayload',
              issue: { ...issue, workflowStateId },
              errors: [],
            },
          },
        })

        const payload = result.data?.issueUpdate

        if (payload === undefined) {
          return UNEXPECTED_RESPONSE
        }

        // `errors` before `issue`: the backend populates exactly one of the
        // two, and the errors are the more specific answer. Joined into one
        // sentence because they all name the same field.
        if (payload.errors.length > 0) {
          return payload.errors.map((error) => error.message).join(' ')
        }

        return payload.issue === null ? UNEXPECTED_RESPONSE : null
      } catch (reason) {
        // The other channel: a rejected promise is an outage or a top-level
        // GraphQL error, never a rejected patch. A failed write is something
        // the board announces, not an exception that takes the page down.
        return describeError(reason)
      }
    },
    [move, workspaceSlug],
  )

  return { moveIssue, isMoving: loading }
}
