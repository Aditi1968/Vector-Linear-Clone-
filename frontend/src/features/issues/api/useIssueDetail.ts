import { useCallback } from 'react'
import { useQuery } from '@apollo/client/react'

import { useWorkspaceSlug } from '../../../app/routes'
import { describeError } from '../lib/errors'
import { IssueDetailDocument } from './documents'
import type { IssueDetailFields } from './types'

/**
 * Canonical hyphenated UUID, which is what `Issue.id` is and therefore what
 * the `UUID!` scalar will accept.
 *
 * Checked before the request rather than after it. An id in the URL comes
 * from whatever the user pasted or edited, and a malformed one fails at
 * *variable coercion* -- a top-level GraphQL error, which would put a
 * "something went wrong" panel in front of someone whose actual situation is
 * that the issue they asked for does not exist. Deciding that here costs one
 * regular expression and turns a misleading error into the correct answer,
 * without a round trip.
 *
 * The version and variant nibbles are left unconstrained. Rejecting a v7 or
 * a nil UUID would be this file inventing a rule the database does not have.
 */
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

export interface UseIssueDetailResult {
  issue: IssueDetailFields | null
  isLoading: boolean
  /**
   * The server answered, and the answer was "no such issue".
   *
   * A real state and not an error: `issue(id:)` is nullable in the schema, so
   * null is a successful response. Kept separate from `errorMessage` because
   * the two want different screens and different recovery -- one offers a way
   * back to the list, the other offers a retry.
   */
  isNotFound: boolean
  errorMessage: string | null
  retry: () => void
}

/**
 * One issue, by the id in the route.
 *
 * Route-backed, which is the point: the id comes from the URL, so a reload of
 * `/issues/<id>` re-runs this query from nothing and the page renders the
 * same way it did before the reload. There is no hand-off of an issue object
 * from the list, and no state that only exists if the user arrived by
 * clicking a row.
 */
export function useIssueDetail(issueId: string | undefined): UseIssueDetailResult {
  // From the route, like the id beside it. `issue(workspaceSlug:, id:)`
  // resolves an id belonging to another workspace to null, so a URL pairing
  // this workspace with somebody else's issue id renders the not-found
  // screen -- the same screen an id that exists nowhere gets, and the same
  // answer the server gives.
  const workspaceSlug = useWorkspaceSlug()

  const isRequestable = issueId !== undefined && UUID_PATTERN.test(issueId)

  const { data, error, loading, refetch } = useQuery(IssueDetailDocument, {
    // `id` still has to type-check when the query is skipped, so an id that
    // will not be sent is passed as the empty string rather than smuggled
    // past the type with a cast.
    variables: { workspaceSlug, id: issueId ?? '' },
    skip: !isRequestable,
  })

  const retry = useCallback(() => {
    // See useIssueList.retry: `error` on the hook result is what the screen
    // renders, so the duplicate rejection carries no extra information.
    void refetch().catch(() => undefined)
  }, [refetch])

  if (!isRequestable) {
    return {
      issue: null,
      isLoading: false,
      isNotFound: true,
      errorMessage: null,
      retry,
    }
  }

  return {
    issue: data?.issue ?? null,
    isLoading: loading,
    // Only once an answer has arrived. Before that `data` is undefined, and
    // reporting "not found" during the first load would flash the wrong
    // screen at every visitor.
    isNotFound: data !== undefined && data.issue === null,
    errorMessage: error === undefined ? null : describeError(error),
    retry,
  }
}
