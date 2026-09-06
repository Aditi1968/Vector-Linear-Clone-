import { useCallback } from 'react'
import { NetworkStatus } from '@apollo/client'
import { useQuery } from '@apollo/client/react'

import { useWorkspaceSlug } from '../../../app/routes'
import {
  CycleDetailDocument,
  CycleIssuesDocument,
  CycleListDocument,
  CycleTeamsDocument,
} from './documents'
import { describeError } from './errors'
import type { CycleFields, CycleIssue, CycleTeam } from './types'

/** See ../../projects/api/queries.ts for why an id is checked before it is sent. */
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

const NO_CYCLES: readonly CycleFields[] = []
const NO_TEAMS: readonly CycleTeam[] = []
const NO_ISSUES: readonly CycleIssue[] = []

export interface UseCycleTeamsResult {
  teams: readonly CycleTeam[]
  isLoading: boolean
  errorMessage: string | null
}

/**
 * The workspace's teams.
 *
 * Not a convenience here but a precondition: `cycles(teamId:)` requires a
 * team and the schema offers no workspace-wide cycle list, so until this
 * answers there is no question to ask. A workspace with no teams therefore
 * has no cycles to show, and the screen says that rather than spinning.
 */
export function useCycleTeams(): UseCycleTeamsResult {
  const workspaceSlug = useWorkspaceSlug()
  const { data, error, loading } = useQuery(CycleTeamsDocument, {
    variables: { workspaceSlug },
  })

  return {
    teams: data?.teams ?? NO_TEAMS,
    isLoading: loading,
    errorMessage: error === undefined ? null : describeError(error),
  }
}

export interface UseCycleListResult {
  cycles: readonly CycleFields[]
  isLoading: boolean
  errorMessage: string | null
  retry: () => void
}

/**
 * One team's cycles.
 *
 * Not paginated, because `cycles` is not a connection: it takes no `first`
 * and returns the team's whole list in `number` order. A team accumulates a
 * couple of dozen cycles a year, so that is the right shape -- and it is the
 * server's shape, not a simplification made here.
 *
 * `teamId` may be undefined while the team query is still answering, and the
 * query is skipped rather than sent with a placeholder: a `UUID!` variable
 * coerced from `''` is a top-level GraphQL error, which would report "not
 * loaded yet" as "something went wrong".
 */
export function useCycleList(teamId: string | undefined): UseCycleListResult {
  const workspaceSlug = useWorkspaceSlug()

  const { data, error, loading, refetch } = useQuery(CycleListDocument, {
    variables: { workspaceSlug, teamId: teamId ?? '' },
    skip: teamId === undefined,
  })

  const retry = useCallback(() => {
    // Swallowed: `refetch` rejects *and* sets `error` on the hook result, and
    // `error` is what the screen renders.
    void refetch().catch(() => undefined)
  }, [refetch])

  return {
    cycles: data?.cycles ?? NO_CYCLES,
    isLoading: loading,
    errorMessage: error === undefined ? null : describeError(error),
    retry,
  }
}

export interface UseCycleDetailResult {
  cycle: CycleFields | null
  isLoading: boolean
  /** The server answered "no such cycle". A successful response, not an error. */
  isNotFound: boolean
  errorMessage: string | null
  retry: () => void
}

/** One cycle, by the id in the route. */
export function useCycleDetail(cycleId: string | undefined): UseCycleDetailResult {
  const workspaceSlug = useWorkspaceSlug()
  const isRequestable = cycleId !== undefined && UUID_PATTERN.test(cycleId)

  const { data, error, loading, refetch } = useQuery(CycleDetailDocument, {
    variables: { workspaceSlug, id: cycleId ?? '' },
    skip: !isRequestable,
  })

  const retry = useCallback(() => {
    void refetch().catch(() => undefined)
  }, [refetch])

  if (!isRequestable) {
    return { cycle: null, isLoading: false, isNotFound: true, errorMessage: null, retry }
  }

  return {
    cycle: data?.cycle ?? null,
    isLoading: loading,
    isNotFound: data !== undefined && data.cycle === null,
    errorMessage: error === undefined ? null : describeError(error),
    retry,
  }
}

export interface UseCycleIssuesResult {
  /** Every workspace issue loaded so far. Filtering by cycle is the caller's job. */
  issues: readonly CycleIssue[]
  loadedCount: number
  hasNextPage: boolean
  isLoading: boolean
  isLoadingMore: boolean
  errorMessage: string | null
  loadMore: () => void
}

/**
 * A page of the workspace's issues, for a cycle screen to filter.
 *
 * ## Why this is not `cycle.issues`
 *
 * Because there is no such field, and no `issues(cycleId:)` argument either.
 * "The issues in this cycle" is a question the API cannot be asked; it can
 * only be answered by fetching issues and matching `issue.cycle.id` here.
 *
 * ## Why it is not even scoped to the cycle's team
 *
 * A cycle belongs to exactly one team, so `issues(teamId:)` would be the
 * right scope -- and `Cycle` exposes no `teamId`. A screen holding a cycle id
 * has no way to recover its team. Passing whichever team the *picker* happened
 * to be on would be worse than not scoping: a cycle opened from a direct link
 * would show nothing at all, and would look empty rather than unscoped.
 *
 * The honest consequence, which the screen states: what is shown is the
 * cycle's issues *among those loaded*, and loading more loads more of the
 * workspace. This is a missing backend filter, not something the UI can be
 * tuned around.
 */
export function useCycleIssues(): UseCycleIssuesResult {
  const workspaceSlug = useWorkspaceSlug()

  const { data, error, networkStatus, fetchMore } = useQuery(CycleIssuesDocument, {
    variables: { workspaceSlug, after: null },
    notifyOnNetworkStatusChange: true,
  })

  const connection = data?.issues
  const endCursor = connection?.pageInfo.endCursor ?? null
  const hasNextPage = connection?.pageInfo.hasNextPage ?? false
  const isLoadingMore = networkStatus === NetworkStatus.fetchMore

  const loadMore = useCallback(() => {
    // Nothing to ask for. Asking anyway would send `after: null`, which the
    // cache's merge policy reads as "start the list over" and which would
    // discard every page already loaded.
    if (!hasNextPage || endCursor === null || isLoadingMore) {
      return
    }

    void fetchMore({ variables: { after: endCursor } }).catch(() => undefined)
  }, [endCursor, fetchMore, hasNextPage, isLoadingMore])

  const issues = connection?.nodes ?? NO_ISSUES

  return {
    issues,
    loadedCount: issues.length,
    hasNextPage,
    isLoading: networkStatus === NetworkStatus.loading,
    isLoadingMore,
    errorMessage: error === undefined ? null : describeError(error),
    loadMore,
  }
}
