import { useCallback } from 'react'
import { useQuery } from '@apollo/client/react'

import { useWorkspaceSlug } from '../../app/routes'
import { describeError } from '../screens'
import { WorkspaceSearchDocument } from '../../generated/operations'
import type { WorkspaceSearchQuery } from '../../generated/operations'

/**
 * The search screen's data adapter.
 *
 * The boundary the screen is written against: it imports the hook and types
 * below and never a document, an Apollo hook, or an Apollo error type. The
 * document lives in ./operations.graphql and is re-exported here only because
 * mocking a response in a test requires the exact document that produced it.
 */

export { WorkspaceSearchDocument } from '../../generated/operations'

/** One issue, as a result row selects it. Narrower than an issue list row. */
export type SearchIssue = WorkspaceSearchQuery['search']['issues'][number]

/** One project, as a result row selects it. */
export type SearchProject = WorkspaceSearchQuery['search']['projects'][number]

/** Stable identities for "nothing yet", so memoised children are not defeated. */
const NO_ISSUES: readonly SearchIssue[] = []
const NO_PROJECTS: readonly SearchProject[] = []

export interface UseSearchResult {
  issues: readonly SearchIssue[]
  projects: readonly SearchProject[]
  /** No query typed. Distinct from "a query that matched nothing". */
  isIdle: boolean
  isLoading: boolean
  errorMessage: string | null
  retry: () => void
}

/**
 * One workspace search.
 *
 * ## The query comes from the URL
 *
 * `useSearch` takes it as an argument rather than owning it, because the
 * screen keeps it in the address bar -- a search that cannot be linked or
 * reloaded is a search you have to retype. The debounce lives there too, for
 * the same reason: it is a decision about when to change the URL, and this
 * hook simply asks for whatever the URL currently says.
 *
 * ## Empty is skipped, not sent
 *
 * `search(query: "")` would be a request with no question in it. `skip`
 * leaves `loading` false, which is what makes `isIdle` distinguishable from
 * "searching" and from "found nothing" -- three states the screen has three
 * different sentences for.
 *
 * ## The string is passed through untouched
 *
 * Trimmed, and nothing else. The backend resolves "ENG-42" to that issue
 * directly; uppercasing, stripping punctuation or splitting on the hyphen
 * here would break the one input this is best at.
 */
export function useSearch(query: string): UseSearchResult {
  const workspaceSlug = useWorkspaceSlug()
  const trimmed = query.trim()

  const { data, error, loading, refetch } = useQuery(WorkspaceSearchDocument, {
    variables: { workspaceSlug, query: trimmed },
    skip: trimmed === '',
  })

  const retry = useCallback(() => {
    // Swallowed on purpose: `refetch` rejects *and* sets `error` on the hook
    // result, and `error` is what the screen renders.
    void refetch().catch(() => undefined)
  }, [refetch])

  return {
    issues: data?.search.issues ?? NO_ISSUES,
    projects: data?.search.projects ?? NO_PROJECTS,
    isIdle: trimmed === '',
    isLoading: loading,
    errorMessage: error === undefined ? null : describeError(error),
    retry,
  }
}
