import { useCallback } from 'react'
import { NetworkStatus } from '@apollo/client'
import { useQuery } from '@apollo/client/react'

import { useWorkspaceSlug } from '../../../app/routes'
import {
  CycleDetailDocument,
  CycleIssuesDocument,
  CycleListDocument,
  CycleTeamsDocument,
  CycleUnscheduledIssuesDocument,
} from './documents'
import { describeError } from './errors'
import type {
  CycleFields,
  CycleIssue,
  CycleTeam,
  CycleUnscheduledIssue,
} from './types'

/** See ../../projects/api/queries.ts for why an id is checked before it is sent. */
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

const NO_CYCLES: readonly CycleFields[] = []
const NO_TEAMS: readonly CycleTeam[] = []
const NO_ISSUES: readonly CycleIssue[] = []
const NO_UNSCHEDULED_ISSUES: readonly CycleUnscheduledIssue[] = []

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
  /** The issues in this cycle, as far as they have been paged in. */
  issues: readonly CycleIssue[]
  /** How many are in the cycle altogether, paging ignored. */
  totalCount: number
  hasNextPage: boolean
  isLoading: boolean
  isLoadingMore: boolean
  errorMessage: string | null
  loadMore: () => void
}

/**
 * The issues in one cycle.
 *
 * `filter: { cycleId }`, so membership is the server's answer rather than a
 * match made here over a page of the workspace. Scoping to the cycle's team
 * as well would be redundant: a cycle belongs to one team, so its issues are
 * that team's already.
 *
 * Still paginated, so `issues` is what has been loaded and `totalCount` is
 * how many there are; the screen states the difference while it exists.
 *
 * The id comes from the route rather than from the loaded cycle, so this asks
 * in the same round trip as `useCycleDetail`; an id that cannot be a UUID is
 * not sent at all.
 */
export function useCycleIssues(cycleId: string | undefined): UseCycleIssuesResult {
  const workspaceSlug = useWorkspaceSlug()
  const isRequestable = cycleId !== undefined && UUID_PATTERN.test(cycleId)

  const { data, error, networkStatus, fetchMore } = useQuery(CycleIssuesDocument, {
    variables: { workspaceSlug, cycleId: cycleId ?? '', after: null },
    skip: !isRequestable,
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

  return {
    issues: connection?.nodes ?? NO_ISSUES,
    totalCount: connection?.totalCount ?? 0,
    hasNextPage,
    isLoading: networkStatus === NetworkStatus.loading,
    isLoadingMore,
    errorMessage: error === undefined ? null : describeError(error),
    loadMore,
  }
}

/**
 * The team's issues that are in no cycle, for the "add an issue" menu.
 *
 * A second query rather than a slice of the panel's list, because the two ask
 * opposite questions -- what is in this cycle, and what is in none. The team
 * is required and comes from `Cycle.teamId`: `issueSetCycle` refuses a cycle
 * that is not the issue's team's, so another team's issue is not a candidate
 * and offering it would be offering a button that cannot work.
 */
export function useCycleUnscheduledIssues(
  teamId: string | undefined,
): readonly CycleUnscheduledIssue[] {
  const workspaceSlug = useWorkspaceSlug()

  const { data } = useQuery(CycleUnscheduledIssuesDocument, {
    variables: { workspaceSlug, teamId: teamId ?? '' },
    skip: teamId === undefined,
  })

  return data?.issues.nodes ?? NO_UNSCHEDULED_ISSUES
}
