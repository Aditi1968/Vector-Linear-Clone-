import { useCallback, useState } from 'react'
import { NetworkStatus } from '@apollo/client'
import { useQuery } from '@apollo/client/react'

import { useWorkspaceSlug } from '../../../app/routes'
import { describeError } from '../../issues/lib/errors'
import { RoadmapInitiativesDocument, RoadmapProjectsDocument } from './documents'
import type { RoadmapInitiative, RoadmapProject } from './types'

const NO_INITIATIVES: readonly RoadmapInitiative[] = []
const NO_PROJECTS: readonly RoadmapProject[] = []

export interface UseRoadmapResult {
  initiatives: readonly RoadmapInitiative[]
  projects: readonly RoadmapProject[]
  /** Either list has a page the roadmap has not drawn. */
  hasMore: boolean
  /** The first fetch of either, with nothing on screen. */
  isLoadingFirstPage: boolean
  isLoadingMore: boolean
  errorMessage: string | null
  loadMoreErrorMessage: string | null
  /** Advance whichever list still has pages. */
  loadMore: () => void
  retry: () => void
}

/**
 * Everything the roadmap draws, from the two lists that carry a target date.
 *
 * ## Two queries, one screen
 *
 * `initiatives` and `projects` are separate connections that page
 * independently, so they are separate operations -- see ./operations.graphql
 * for why one document holding both would silently drop pages the moment one
 * list ran out.
 *
 * "Load more" advances *both* where both have more, because a roadmap missing
 * half its later items is not a roadmap. The two cursors are tracked
 * separately and a list that is exhausted is simply not asked again; sending
 * it `after: null` would be read by the merge policy as "start this list
 * over" and would drop every page of it already loaded.
 *
 * ## Loading is all-or-nothing on purpose
 *
 * `isLoadingFirstPage` stays true until both answers are in. Rendering half a
 * calendar and then re-laying it out when the other half lands is worse than
 * a moment of skeleton: the months would visibly rearrange under the reader's
 * eye.
 */
export function useRoadmap(): UseRoadmapResult {
  const workspaceSlug = useWorkspaceSlug()
  const [loadMoreErrorMessage, setLoadMoreErrorMessage] = useState<string | null>(null)

  const initiativesQuery = useQuery(RoadmapInitiativesDocument, {
    variables: { workspaceSlug, after: null },
    notifyOnNetworkStatusChange: true,
  })

  const projectsQuery = useQuery(RoadmapProjectsDocument, {
    variables: { workspaceSlug, after: null },
    notifyOnNetworkStatusChange: true,
  })

  const initiativeConnection = initiativesQuery.data?.initiatives
  const projectConnection = projectsQuery.data?.projects

  const initiativeCursor = initiativeConnection?.pageInfo.endCursor ?? null
  const projectCursor = projectConnection?.pageInfo.endCursor ?? null

  const initiativesHaveMore = initiativeConnection?.pageInfo.hasNextPage ?? false
  const projectsHaveMore = projectConnection?.pageInfo.hasNextPage ?? false

  const isLoadingMore =
    initiativesQuery.networkStatus === NetworkStatus.fetchMore ||
    projectsQuery.networkStatus === NetworkStatus.fetchMore

  const loadMore = useCallback(() => {
    if (isLoadingMore) {
      return
    }

    setLoadMoreErrorMessage(null)

    const onFailure = (reason: unknown) => {
      // Caught rather than left to reject: a failed "load more" must not
      // clear the months already on screen.
      setLoadMoreErrorMessage(describeError(reason))
    }

    if (initiativesHaveMore && initiativeCursor !== null) {
      void initiativesQuery
        .fetchMore({ variables: { after: initiativeCursor } })
        .catch(onFailure)
    }

    if (projectsHaveMore && projectCursor !== null) {
      void projectsQuery
        .fetchMore({ variables: { after: projectCursor } })
        .catch(onFailure)
    }
  }, [
    initiativeCursor,
    initiativesHaveMore,
    initiativesQuery,
    isLoadingMore,
    projectCursor,
    projectsHaveMore,
    projectsQuery,
  ])

  const retry = useCallback(() => {
    setLoadMoreErrorMessage(null)
    void initiativesQuery.refetch().catch(() => undefined)
    void projectsQuery.refetch().catch(() => undefined)
  }, [initiativesQuery, projectsQuery])

  // The first error either query reported. Two panels saying the network is
  // down is not twice as informative as one.
  const failure = initiativesQuery.error ?? projectsQuery.error

  return {
    initiatives: initiativeConnection?.nodes ?? NO_INITIATIVES,
    projects: projectConnection?.nodes ?? NO_PROJECTS,
    hasMore: initiativesHaveMore || projectsHaveMore,
    isLoadingFirstPage:
      failure === undefined &&
      (initiativeConnection === undefined || projectConnection === undefined),
    isLoadingMore,
    errorMessage: failure === undefined ? null : describeError(failure),
    loadMoreErrorMessage,
    loadMore,
    retry,
  }
}
