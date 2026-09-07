import { useCallback } from 'react'
import { useQuery } from '@apollo/client/react'

import { useWorkspaceSlug } from '../../../app/routes'
import { describeError } from '../../issues/lib/errors'
import { TriageQueueDocument } from './documents'
import type { TriageRow } from './types'

const NO_ROWS: readonly TriageRow[] = []

export interface UseTriageQueueResult {
  /** The oldest page of the queue. See below for why there is only one. */
  rows: readonly TriageRow[]
  /**
   * How long the whole queue is, paging ignored.
   *
   * From `triageCount`, not from `rows.length`: `TriageIssueConnection` has
   * no `totalCount`, and the two numbers are different facts. The screen
   * states the difference whenever one exists rather than letting a page of
   * 25 read as a queue of 25.
   */
  totalCount: number
  /** The server has more than this page. Reported, not offered as a button. */
  hasNextPage: boolean
  isLoading: boolean
  errorMessage: string | null
  retry: () => void
}

/**
 * One team's triage queue, oldest first.
 *
 * ## Why this loads one page and offers no "Load more"
 *
 * `fetchMore` on this field would send a request and change nothing on
 * screen, silently. `src/lib/graphql/cache.ts` installs
 * `cursorConnectionPolicy` on `issues`, `labels`, `projects` and
 * `notifications` and on three connections of `Issue`; `triageIssues` has no
 * policy, so Apollo's default keys the field on *every* argument, `after`
 * included. Page two lands under a cache key no mounted query watches, and
 * the button becomes a lie. Apollo does not warn about this.
 *
 * The obvious fix -- reuse `cursorConnectionPolicy` -- does not work here
 * and would be worse than the bug. That merge dedupes with
 * `readField('id', node) ?? node.__ref`, which assumes every node is a
 * normalised entity. `TriageIssue` is `{ issue, enteredAt }` and has no
 * `id`, so it is stored inline rather than as a `Reference`: both halves of
 * that expression are `undefined` for every node, the first row would put
 * `undefined` in the `seen` set, and every later row on the page would be
 * dropped as a duplicate. A correct policy needs a merge that keys on
 * `issue.id`, which is a new function in a shared file rather than a
 * three-line addition -- so it is reported rather than smuggled in here.
 *
 * Meanwhile this is not a crippled screen. A triage queue is worked from its
 * oldest end and refills as rows leave it: every accept, decline, duplicate
 * and team change refetches this query, so the next waiting issues page into
 * view as the ones above them are dealt with. The header states the whole
 * count from `triageCount`, so nobody mistakes a page for the queue.
 *
 * `teamId` may be undefined while the workspace context is still answering,
 * and the query is skipped rather than sent with a placeholder: a `UUID!`
 * variable coerced from `''` is a top-level GraphQL error, which would report
 * "not loaded yet" to the user as "something went wrong".
 */
export function useTriageQueue(teamId: string | undefined): UseTriageQueueResult {
  const workspaceSlug = useWorkspaceSlug()

  const { data, error, loading, refetch } = useQuery(TriageQueueDocument, {
    variables: { workspaceSlug, teamId: teamId ?? '', after: null },
    skip: teamId === undefined,
  })

  const retry = useCallback(() => {
    // Swallowed: `refetch` rejects *and* sets `error` on the hook result, and
    // `error` is what the screen renders.
    void refetch().catch(() => undefined)
  }, [refetch])

  return {
    rows: data?.triageIssues.nodes ?? NO_ROWS,
    totalCount: data?.triageCount ?? 0,
    hasNextPage: data?.triageIssues.pageInfo.hasNextPage ?? false,
    isLoading: loading,
    errorMessage: error === undefined ? null : describeError(error),
    retry,
  }
}
