import { useCallback, useState } from 'react'
import { NetworkStatus } from '@apollo/client'
import { useQuery } from '@apollo/client/react'

import { useWorkspaceSlug } from '../../../app/routes'
import {
  ProjectDetailDocument,
  ProjectIssuesDocument,
  ProjectListDocument,
  ProjectMembersDocument,
  ProjectTeamsDocument,
  ProjectUnfiledIssuesDocument,
} from './documents'
import { describeError } from './errors'
import type {
  ProjectDetailFields,
  ProjectIssue,
  ProjectMember,
  ProjectRowFields,
  ProjectTeam,
  ProjectUnfiledIssue,
} from './types'

/**
 * Canonical hyphenated UUID.
 *
 * Checked before the request rather than after it. An id in the URL comes
 * from whatever was pasted or edited, and a malformed one fails at *variable
 * coercion* -- a top-level GraphQL error, which would put a "something went
 * wrong" panel in front of someone whose actual situation is that the
 * project they asked for does not exist.
 */
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

/** Stable identities for "nothing yet", so memoised children are not defeated. */
const NO_PROJECTS: readonly ProjectRowFields[] = []
const NO_TEAMS: readonly ProjectTeam[] = []
const NO_MEMBERS: readonly ProjectMember[] = []
const NO_ISSUES: readonly ProjectIssue[] = []
const NO_UNFILED_ISSUES: readonly ProjectUnfiledIssue[] = []

export interface UseProjectListResult {
  projects: readonly ProjectRowFields[]
  hasNextPage: boolean
  /** The very first fetch, with nothing on screen. */
  isLoadingFirstPage: boolean
  /** A `fetchMore`, with rows already on screen. Deliberately separate. */
  isLoadingMore: boolean
  isRefreshing: boolean
  errorMessage: string | null
  loadMoreErrorMessage: string | null
  loadMore: () => void
  retry: () => void
}

/**
 * The project list, paginated by cursor.
 *
 * Pages accumulate in the *cache*, not here: `src/lib/graphql/cache.ts` owns
 * the `projects` field policy that merges them, and it is the only thing
 * that does. A hook that also concatenated pages would double every row the
 * moment both ran, which is how "load more shows duplicates" usually gets
 * built and is invisible until there is a second page.
 *
 * The workspace comes from the URL rather than from an argument. No screen
 * rendering this list has a workspace to pass that the URL does not already
 * state, and a prop would let two components on one page disagree about
 * which tenant they are showing.
 */
export function useProjectList(): UseProjectListResult {
  const workspaceSlug = useWorkspaceSlug()
  const [loadMoreErrorMessage, setLoadMoreErrorMessage] = useState<string | null>(null)

  const { data, error, networkStatus, fetchMore, refetch } = useQuery(
    ProjectListDocument,
    {
      // `after: null` stated rather than omitted: the merge policy reads
      // `args.after` to decide whether a result starts the list or extends
      // it, and the decision should read a value the document declares.
      variables: { workspaceSlug, after: null },
      notifyOnNetworkStatusChange: true,
    },
  )

  const connection = data?.projects
  const endCursor = connection?.pageInfo.endCursor ?? null
  const hasNextPage = connection?.pageInfo.hasNextPage ?? false
  const isLoadingMore = networkStatus === NetworkStatus.fetchMore

  const loadMore = useCallback(() => {
    // `hasNextPage` false is the end of the connection, and a null cursor
    // would mean the server had no position to resume from. Asking anyway
    // would send `after: null`, which the merge policy reads as "start the
    // list over" and which would wipe every page already loaded.
    if (!hasNextPage || endCursor === null || isLoadingMore) {
      return
    }

    setLoadMoreErrorMessage(null)

    void fetchMore({ variables: { after: endCursor } }).catch((reason: unknown) => {
      // Caught rather than left to reject: a failed "load more" must not
      // clear the rows already on screen, and an unhandled rejection would
      // be reported as a page-level crash by any error tracker.
      setLoadMoreErrorMessage(describeError(reason))
    })
  }, [endCursor, fetchMore, hasNextPage, isLoadingMore])

  const retry = useCallback(() => {
    setLoadMoreErrorMessage(null)
    // The rejection is swallowed on purpose: `refetch` rejects *and* sets
    // `error` on the hook result, and `error` is what the screen renders.
    void refetch().catch(() => undefined)
  }, [refetch])

  return {
    projects: connection?.nodes ?? NO_PROJECTS,
    hasNextPage,
    isLoadingFirstPage: networkStatus === NetworkStatus.loading,
    isLoadingMore,
    isRefreshing: networkStatus === NetworkStatus.refetch,
    errorMessage: error === undefined ? null : describeError(error),
    loadMoreErrorMessage,
    loadMore,
    retry,
  }
}

export interface UseProjectDetailResult {
  project: ProjectDetailFields | null
  isLoading: boolean
  /**
   * The server answered, and the answer was "no such project".
   *
   * A real state and not an error: `project(id:)` is nullable, so null is a
   * successful response. Kept separate from `errorMessage` because the two
   * want different screens and different recovery -- one offers a way back
   * to the list, the other offers a retry.
   */
  isNotFound: boolean
  errorMessage: string | null
  retry: () => void
}

/** One project, by the id in the route. */
export function useProjectDetail(projectId: string | undefined): UseProjectDetailResult {
  // `project(workspaceSlug:, id:)` resolves an id belonging to another
  // workspace to null, so a URL pairing this workspace with somebody else's
  // project id gets the not-found screen -- the same answer the server gives,
  // and the same one an id that exists nowhere gets.
  const workspaceSlug = useWorkspaceSlug()
  const isRequestable = projectId !== undefined && UUID_PATTERN.test(projectId)

  const { data, error, loading, refetch } = useQuery(ProjectDetailDocument, {
    // The id still has to type-check when the query is skipped, so one that
    // will not be sent is passed as `''` rather than cast past the type.
    variables: { workspaceSlug, id: projectId ?? '' },
    skip: !isRequestable,
  })

  const retry = useCallback(() => {
    void refetch().catch(() => undefined)
  }, [refetch])

  if (!isRequestable) {
    return { project: null, isLoading: false, isNotFound: true, errorMessage: null, retry }
  }

  return {
    project: data?.project ?? null,
    isLoading: loading,
    // Only once an answer has arrived. Before that `data` is undefined, and
    // reporting "not found" during the first load flashes the wrong screen
    // at every visitor.
    isNotFound: data !== undefined && data.project === null,
    errorMessage: error === undefined ? null : describeError(error),
    retry,
  }
}

export interface UseProjectTeamsResult {
  teams: readonly ProjectTeam[]
  isLoading: boolean
  errorMessage: string | null
}

/**
 * The workspace's teams.
 *
 * The only thing that can turn `Project.teamIds` -- a list of bare UUIDs --
 * into names a person recognises, and the source of the add-a-team picker.
 */
export function useProjectTeams(): UseProjectTeamsResult {
  const workspaceSlug = useWorkspaceSlug()
  const { data, error, loading } = useQuery(ProjectTeamsDocument, {
    variables: { workspaceSlug },
  })

  return {
    teams: data?.teams ?? NO_TEAMS,
    isLoading: loading,
    errorMessage: error === undefined ? null : describeError(error),
  }
}

export interface UseProjectMembersResult {
  /**
   * Everyone the workspace has had, for turning a `leadId` into a name.
   *
   * Includes people who have left. A project whose lead was since removed
   * still has a lead in the data, and dropping them here would render it as
   * led by nobody rather than by somebody who is gone.
   */
  members: readonly ProjectMember[]
  /** The people still here -- the only ones the lead picker may offer. */
  activeMembers: readonly ProjectMember[]
  isLoading: boolean
  /**
   * Deliberately not surfaced as a page-level error.
   *
   * A member list that fails to load costs a lead's *name*, and the screen
   * degrades to saying the lead is someone it cannot identify. Taking the
   * whole project page down over it would be a worse answer than that.
   */
  errorMessage: string | null
}

/**
 * The workspace's members, for resolving and choosing a project lead.
 *
 * `Project.leadId` is a bare UUID and there is no `Project.lead` field, so
 * this is the only thing standing between a person's name and a UUID printed
 * on screen.
 */
export function useProjectMembers(): UseProjectMembersResult {
  const workspaceSlug = useWorkspaceSlug()
  const { data, error, loading } = useQuery(ProjectMembersDocument, {
    variables: { workspaceSlug },
  })

  const members = data?.workspaceMembers ?? NO_MEMBERS

  return {
    members,
    activeMembers: members.filter((member) => member.removedAt === null),
    isLoading: loading,
    errorMessage: error === undefined ? null : describeError(error),
  }
}

export interface UseProjectIssuesResult {
  /** The issues in this project, as far as they have been paged in. */
  issues: readonly ProjectIssue[]
  /** How many are in the project altogether, paging ignored. */
  totalCount: number
  hasNextPage: boolean
  isLoading: boolean
  isLoadingMore: boolean
  errorMessage: string | null
  loadMore: () => void
}

/**
 * The issues in one project.
 *
 * `filter: { projectId }`, so membership is decided by the server and this
 * hook returns the project's issues rather than the workspace's for a screen
 * to sift. `teamId` is still not sent, deliberately: a project spans teams
 * (`Project.teamIds` is a list), so scoping to one would hide the issues of
 * every other team working on it.
 *
 * What has not changed is that the answer is paginated, so `issues` is the
 * project's issues *so far* and `totalCount` is how many there are. The
 * screen states the difference while it exists.
 *
 * The id comes from the route rather than from the loaded project, so this
 * asks in the same round trip as `useProjectDetail` rather than waiting for
 * it; an id that cannot be a UUID is not sent at all, for the reason
 * `UUID_PATTERN` is declared above.
 */
export function useProjectIssues(projectId: string | undefined): UseProjectIssuesResult {
  const workspaceSlug = useWorkspaceSlug()
  const isRequestable = projectId !== undefined && UUID_PATTERN.test(projectId)

  const { data, error, networkStatus, fetchMore } = useQuery(ProjectIssuesDocument, {
    variables: { workspaceSlug, projectId: projectId ?? '', after: null },
    skip: !isRequestable,
    notifyOnNetworkStatusChange: true,
  })

  const connection = data?.issues
  const endCursor = connection?.pageInfo.endCursor ?? null
  const hasNextPage = connection?.pageInfo.hasNextPage ?? false
  const isLoadingMore = networkStatus === NetworkStatus.fetchMore

  const loadMore = useCallback(() => {
    if (!hasNextPage || endCursor === null || isLoadingMore) {
      return
    }

    // Swallowed: a failed extension leaves the issues already matched on
    // screen, and the panel has no separate place to report it.
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
 * The issues no project has claimed, for the "add an issue" menu.
 *
 * A second query rather than a slice of the panel's own list, because the two
 * ask opposite questions: the panel wants this project's issues and the menu
 * wants the ones in no project at all. Server-side filtering makes them
 * genuinely different requests, and the menu's is the better list for it --
 * it is candidates from the whole workspace rather than whichever unfiled
 * issues happened to share a page with this project's.
 */
export function useUnfiledIssues(): readonly ProjectUnfiledIssue[] {
  const workspaceSlug = useWorkspaceSlug()

  const { data } = useQuery(ProjectUnfiledIssuesDocument, {
    variables: { workspaceSlug },
  })

  return data?.issues.nodes ?? NO_UNFILED_ISSUES
}
