import { useCallback, useState } from 'react'
import { NetworkStatus } from '@apollo/client'
import { useQuery } from '@apollo/client/react'

import { useWorkspaceSlug } from '../../../app/routes'
import { describeError } from '../../issues/lib/errors'
import { WorkspaceAnalyticsDocument } from './documents'
import type { WorkspaceAnalytics } from './types'

/**
 * The windows the picker offers, in days.
 *
 * Every one is inside `ANALYTICS_MAX_DAYS` (180), which the server enforces
 * and refuses rather than clamps. The list is short on purpose: a free-text
 * day count is a control whose only interesting values are these four, and
 * whose interesting *invalid* values produce an error message instead of a
 * chart.
 */
export const RANGE_OPTIONS = [7, 30, 90, 180] as const

export type RangeDays = (typeof RANGE_OPTIONS)[number]

export const DEFAULT_RANGE: RangeDays = 30

/** The widest window the server will answer, restated for the screen. */
export const MAX_RANGE_DAYS = 180

export interface UseWorkspaceAnalyticsResult {
  analytics: WorkspaceAnalytics | null
  days: RangeDays
  setDays: (days: RangeDays) => void
  /** The first fetch, with nothing on screen yet. */
  isLoadingFirstPage: boolean
  /** A refetch for a different window, with the previous one still drawn. */
  isRefetching: boolean
  errorMessage: string | null
  retry: () => void
}

/**
 * Everything the analytics screen reads, for one window.
 *
 * ## Why the two loading flags are separate
 *
 * `isLoadingFirstPage` means nothing is on screen. `isRefetching` means a
 * wider window is in flight and the previous one is still drawn. They are
 * different states and they get different renderings, for the reason the
 * roadmap's two flags are separate: a chart that blanks itself every time the
 * reader widens the range is worse than one that dims, because the reader
 * loses the thing they were comparing against.
 *
 * `notifyOnNetworkStatusChange` is what makes `setVariables` observable at
 * all; without it a range change is silent and the screen shows stale numbers
 * under a new heading with nothing to say they are stale.
 *
 * ## There is no pagination here and no cursor
 *
 * Nothing on this screen is a connection. Every list the aggregate returns is
 * bounded server-side -- twenty assignees, fifty teams, twenty-five projects,
 * twelve cycles -- and each arrives beside a `*Total` saying how many there
 * really are. A "load more" would be a second aggregate over a second
 * snapshot, so the screen says "25 of 118" instead of offering one.
 */
export function useWorkspaceAnalytics(): UseWorkspaceAnalyticsResult {
  const workspaceSlug = useWorkspaceSlug()
  const [days, setDays] = useState<RangeDays>(DEFAULT_RANGE)

  const query = useQuery(WorkspaceAnalyticsDocument, {
    variables: { workspaceSlug, days },
    notifyOnNetworkStatusChange: true,
  })

  const retry = useCallback(() => {
    void query.refetch().catch(() => undefined)
  }, [query])

  /*
    `previousData` is what makes "dims rather than blanks" true rather than
    merely intended. Apollo clears `data` the moment a variable changes, so
    reading only `data` would drop every figure to null on a range change --
    and the screen would fall back to its skeleton, which is exactly the
    behaviour `isRefetching` exists to avoid. The stale window stays drawn,
    dimmed, until the new one lands.
  */
  const analytics =
    query.data?.workspaceAnalytics ?? query.previousData?.workspaceAnalytics ?? null

  return {
    analytics,
    days,
    setDays,
    // Nothing drawn AND nothing wrong: the skeleton state. An error with no
    // data is the error state, not a permanent skeleton.
    isLoadingFirstPage: query.error === undefined && analytics === null,
    isRefetching:
      query.networkStatus === NetworkStatus.setVariables ||
      query.networkStatus === NetworkStatus.refetch,
    errorMessage: query.error === undefined ? null : describeError(query.error),
    retry,
  }
}
