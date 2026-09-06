import { useCallback } from 'react'
import { useMutation } from '@apollo/client/react'

import { useWorkspaceSlug } from '../../../app/routes'
import { prependCreatedIssue } from './cache'
import { IssueCreateDocument } from './documents'
import { failureOutcome, interpretPayload } from './outcome'
import type { IssueSaveOutcome } from './outcome'
import type { IssueDraft } from './types'
import { useWorkspaceContext } from './useWorkspaceContext'

/**
 * There is no team to file against.
 *
 * A real state and not a transport failure: `issueCreate` requires a
 * `teamId` and the server no longer picks one, so a workspace with no teams
 * -- or a context query that has not answered yet -- has nothing to file
 * into. Reported as a `failed` outcome because it is a fact about the
 * workspace and not about anything the person typed, so no field owns it.
 */
const NO_TEAM = 'This workspace has no team to file issues in.'

export interface UseCreateIssueResult {
  createIssue: (draft: IssueDraft) => Promise<IssueSaveOutcome>
  isSubmitting: boolean
}

/**
 * Create an issue and get the list to agree, without reloading anything.
 *
 * The cache write lives in ./cache.ts and its reasoning is there. What
 * belongs here is why it happens in `update` rather than after `await`:
 * `update` runs inside the mutation's cache transaction, so the new row and
 * the mutation's own normalised result land in one broadcast and the list
 * re-renders once. Doing it afterwards would work and would flicker.
 *
 * ## Where the workspace and the team come from
 *
 * `IssueCreateInput` requires both, and neither is something the person
 * filling in the form is asked. The workspace is the one in the URL. The
 * team is the FIRST the workspace has, which is a placeholder and is marked
 * as one: a workspace with several teams has a product decision behind it
 * that nobody has made -- a picker, a per-user default, a remembered last
 * choice -- and picking the first is the smallest thing that files an issue
 * correctly for the single-team case every workspace starts in.
 *
 * The teams come from `useWorkspaceContext`, which the surrounding screen is
 * already reading to resolve statuses and assignees, so opening the composer
 * costs no request of its own and the answer is there before the button is
 * pressed -- which is what lets the failure be a disabled state rather than
 * a rejected submission.
 */
export function useCreateIssue(): UseCreateIssueResult {
  const workspaceSlug = useWorkspaceSlug()
  const { teams } = useWorkspaceContext()

  // ponytail: first team wins. Replace with a picker (or a remembered
  // default) when a workspace in this product routinely has more than one.
  const teamId = teams[0]?.id

  const [mutate, { loading }] = useMutation(IssueCreateDocument, {
    update(cache, result) {
      const created = result.data?.issueCreate.issue

      // Null when the server rejected the input. There is no issue to add,
      // and `errors` is the form's business, not the cache's.
      if (created == null) {
        return
      }

      prependCreatedIssue(cache, created, workspaceSlug)
    },
  })

  const createIssue = useCallback(
    async (draft: IssueDraft): Promise<IssueSaveOutcome> => {
      if (teamId === undefined) {
        return { status: 'failed', message: NO_TEAM }
      }

      try {
        const result = await mutate({
          variables: { input: { ...draft, workspaceSlug, teamId } },
        })

        return interpretPayload(result.data?.issueCreate)
      } catch (reason) {
        return failureOutcome(reason)
      }
    },
    [mutate, teamId, workspaceSlug],
  )

  return { createIssue, isSubmitting: loading }
}
