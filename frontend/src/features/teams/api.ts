import { useCallback, useState } from 'react'
import { NetworkStatus } from '@apollo/client'
import { useQuery } from '@apollo/client/react'
import { useParams } from 'react-router-dom'

import { TEAM_KEY_PARAM, useWorkspaceSlug } from '../../app/routes'
import { describeError } from '../screens'
import { TeamBoardsDocument, TeamIssueListDocument } from '../../generated/operations'
import type { IssueRowFieldsFragment, TeamBoardsQuery } from '../../generated/operations'

/**
 * The team screens' data adapter.
 *
 * The boundary the two screens are written against: they import the hooks and
 * types below and never a document, an Apollo hook, or an Apollo error type.
 * The documents live in ./operations.graphql and are re-exported here only
 * because mocking a response in a test requires the exact document that
 * produced it.
 */

export { TeamBoardsDocument, TeamIssueListDocument } from '../../generated/operations'

/** One team, with the board its issues move across. */
export type TeamBoard = TeamBoardsQuery['teams'][number]

/** One status on that board. Branch on `category`, never on `name`. */
export type TeamWorkflowState = TeamBoard['workflowStates'][number]

/** Stable identities for "nothing yet", so memoised children are not defeated. */
const NO_ISSUES: readonly IssueRowFieldsFragment[] = []

/**
 * What the URL's team key resolved to.
 *
 * Four cases, as a discriminated union rather than three booleans and a
 * nullable team, so a screen cannot render the not-found state while the
 * request is still in flight or treat a failure as a missing team. Those two
 * are the mistakes worth making impossible: they look identical on screen and
 * mean opposite things.
 */
export type TeamResolution =
  | { status: 'loading' }
  | { status: 'failed'; message: string; retry: () => void }
  | { status: 'missing'; key: string }
  | { status: 'found'; team: TeamBoard; teams: readonly TeamBoard[] }

/**
 * The team the URL names, by key.
 *
 * ## Why a key and not an id
 *
 * `paths.team(slug, teamKey)` builds `/:workspaceSlug/team/ENG`, because ENG
 * is the name the team is known by outside the product -- it is the prefix of
 * every one of its issue identifiers. No root field accepts one, so the
 * resolution is a lookup over `teams(workspaceSlug:)` and there is no
 * cheaper way to do it.
 *
 * Compared case-insensitively. The schema guarantees keys are uppercase, so
 * this only ever widens what a hand-typed or lowercased URL will match; it
 * cannot make two teams collide, because uppercased keys are unique within
 * the workspace by the same guarantee.
 *
 * ## A key that names no team is not an error
 *
 * It is an ordinary answer -- a mistyped URL, a team that was renamed -- and
 * `missing` is how it is reported. A *failed* request is `failed`, and the
 * two are kept apart because the screen owes the user a different sentence
 * and a different next action for each.
 */
export function useTeamByKey(): TeamResolution {
  const workspaceSlug = useWorkspaceSlug()
  const teamKey = useParams()[TEAM_KEY_PARAM] ?? ''

  const { data, error, refetch } = useQuery(TeamBoardsDocument, {
    variables: { workspaceSlug },
  })

  const retry = useCallback(() => {
    // Swallowed on purpose: `refetch` rejects *and* sets `error` on the hook
    // result, and `error` is what the screen renders.
    void refetch().catch(() => undefined)
  }, [refetch])

  if (data === undefined) {
    if (error !== undefined) {
      return { status: 'failed', message: describeError(error), retry }
    }

    return { status: 'loading' }
  }

  const wanted = teamKey.toUpperCase()
  const team = data.teams.find((candidate) => candidate.key.toUpperCase() === wanted)

  if (team === undefined) {
    return { status: 'missing', key: teamKey }
  }

  return { status: 'found', team, teams: data.teams }
}

export interface UseTeamIssuesResult {
  issues: readonly IssueRowFieldsFragment[]
  hasNextPage: boolean
  isLoadingFirstPage: boolean
  isLoadingMore: boolean
  isRefreshing: boolean
  errorMessage: string | null
  loadMoreErrorMessage: string | null
  loadMore: () => void
  retry: () => void
}

/**
 * One team's issues, paginated by cursor.
 *
 * `teamId` may be null, which is the state before the key has been resolved:
 * the query is skipped rather than sent with an empty id, because a malformed
 * UUID fails at variable coercion and arrives as a top-level GraphQL error --
 * a "something went wrong" panel in front of someone whose actual situation
 * is that the page has not finished loading.
 *
 * Pages accumulate in the cache, not here. `src/lib/graphql/cache.ts` owns
 * the merge and it is the only thing that does; a hook that also concatenated
 * would double every row the moment both ran.
 */
export function useTeamIssues(teamId: string | null): UseTeamIssuesResult {
  const workspaceSlug = useWorkspaceSlug()
  const [loadMoreErrorMessage, setLoadMoreErrorMessage] = useState<string | null>(null)

  const { data, error, networkStatus, fetchMore, refetch } = useQuery(
    TeamIssueListDocument,
    {
      // `after: null` stated rather than omitted: the merge policy reads
      // `args.after` to decide whether a result starts the list or extends it.
      variables: { workspaceSlug, teamId: teamId ?? '', after: null },
      skip: teamId === null,
      notifyOnNetworkStatusChange: true,
    },
  )

  const connection = data?.issues
  const endCursor = connection?.pageInfo.endCursor ?? null
  const hasNextPage = connection?.pageInfo.hasNextPage ?? false
  const isLoadingMore = networkStatus === NetworkStatus.fetchMore

  const loadMore = useCallback(() => {
    // Asking past the end would send `after: null`, which the merge policy
    // reads as "start the list over" and which would wipe every page loaded.
    if (!hasNextPage || endCursor === null || isLoadingMore) {
      return
    }

    setLoadMoreErrorMessage(null)

    void fetchMore({ variables: { after: endCursor } }).catch((reason: unknown) => {
      // A failed "load more" must not clear the rows already on screen, and
      // an unhandled rejection would be reported as a page-level crash.
      setLoadMoreErrorMessage(describeError(reason))
    })
  }, [endCursor, fetchMore, hasNextPage, isLoadingMore])

  const retry = useCallback(() => {
    setLoadMoreErrorMessage(null)
    void refetch().catch(() => undefined)
  }, [refetch])

  return {
    issues: connection?.nodes ?? NO_ISSUES,
    hasNextPage,
    // `skip` leaves `networkStatus` at `ready`, so a skipped query is not a
    // loading one -- which is right: the team key has not resolved yet and
    // the caller is showing its own state for that.
    isLoadingFirstPage: networkStatus === NetworkStatus.loading,
    isLoadingMore,
    isRefreshing: networkStatus === NetworkStatus.refetch,
    errorMessage: error === undefined ? null : describeError(error),
    loadMoreErrorMessage,
    loadMore,
    retry,
  }
}
