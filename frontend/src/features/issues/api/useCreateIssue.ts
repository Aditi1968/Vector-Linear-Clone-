import { useCallback } from 'react'
import { useMutation, useQuery } from '@apollo/client/react'

import { useWorkspaceSlug } from '../../../app/routes'
import { describeError } from '../lib/errors'
import { prependCreatedIssue } from './cache'
import { IssueCreateDocument, WorkspaceTeamsDocument } from './documents'
import type {
  IssueDraft,
  IssueDetailFields,
  IssueValidationError,
} from './types'

/**
 * Nothing came back that this code knows how to interpret. Distinct from a
 * validation rejection and from a transport failure, and rare enough that a
 * bespoke sentence would be more alarming than useful.
 */
const UNEXPECTED_RESPONSE =
  'The issue could not be created. Please try again.'

/**
 * There is no team to file against.
 *
 * A real state and not a transport failure: `issueCreate` requires a
 * `teamId` and the server no longer picks one, so a workspace with no teams
 * -- or a teams query that has not answered yet -- has nothing to file
 * into. Reported as a `failed` outcome because it is a fact about the
 * workspace and not about anything the person typed, so no field owns it.
 */
const NO_TEAM =
  'This workspace has no team to file issues in.'

/**
 * What creating an issue can do, as three cases that cannot be confused.
 *
 * The two failure cases are genuinely different things and the backend
 * returns them through genuinely different channels:
 *
 *   - `rejected` is `IssueCreatePayload.errors`, a typed list arriving inside
 *     `data` over a successful response. It means the input was wrong -- an
 *     empty or over-long title, a priority outside 0..4 -- and every entry
 *     names the field it is about. The form shows these next to the fields.
 *   - `failed` is a rejected promise: a network failure, an outage, a bug.
 *     No field owns it, so the form shows it once, at the top.
 *
 * A discriminated union rather than `{ ok, errors, message }` so that the
 * caller cannot read `errors` out of a success or forget to check one of the
 * two failure paths -- the compiler makes it check.
 */
export type CreateIssueOutcome =
  | { status: 'created'; issue: IssueDetailFields }
  | { status: 'rejected'; errors: readonly IssueValidationError[] }
  | { status: 'failed'; message: string }

export interface UseCreateIssueResult {
  createIssue: (draft: IssueDraft) => Promise<CreateIssueOutcome>
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
 * team is the FIRST the workspace has, which is a placeholder and is
 * marked as one: a workspace with several teams has a product decision
 * behind it that nobody has made -- a picker, a per-user default, a
 * remembered last choice -- and picking the first is the smallest thing
 * that files an issue correctly for the single-team case every workspace
 * starts in.
 *
 * The teams query runs while the composer is mounted rather than at submit
 * time. It is two fields on one row and it is what decides whether the form
 * can be submitted at all, so having the answer before the button is pressed
 * is what lets the failure be a disabled state rather than a rejected
 * submission.
 */
export function useCreateIssue(): UseCreateIssueResult {
  const workspaceSlug = useWorkspaceSlug()

  const { data: teams } = useQuery(WorkspaceTeamsDocument, {
    variables: { workspaceSlug },
  })

  // ponytail: first team wins. Replace with a picker (or a remembered
  // default) when a workspace in this product routinely has more than one.
  const teamId = teams?.teams[0]?.id

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
    async (draft: IssueDraft): Promise<CreateIssueOutcome> => {
      if (teamId === undefined) {
        return { status: 'failed', message: NO_TEAM }
      }

      try {
        const result = await mutate({
          variables: { input: { ...draft, workspaceSlug, teamId } },
        })
        const payload = result.data?.issueCreate

        if (payload === undefined) {
          return { status: 'failed', message: UNEXPECTED_RESPONSE }
        }

        // Checked before `issue`, because the backend's contract is that
        // exactly one of the two is populated and the errors are the more
        // specific answer.
        if (payload.errors.length > 0) {
          return { status: 'rejected', errors: payload.errors }
        }

        if (payload.issue === null) {
          return { status: 'failed', message: UNEXPECTED_RESPONSE }
        }

        return { status: 'created', issue: payload.issue }
      } catch (reason) {
        // The default `errorPolicy` of `none` makes `mutate` reject on a
        // top-level GraphQL error as well as on a transport failure, so this
        // is the only place either can arrive. Returned rather than re-thrown:
        // a failed create is an outcome the form renders, not an exception
        // that should take the page down.
        return { status: 'failed', message: describeError(reason) }
      }
    },
    [mutate, teamId, workspaceSlug],
  )

  return { createIssue, isSubmitting: loading }
}
