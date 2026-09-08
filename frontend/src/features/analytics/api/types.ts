/**
 * The names this feature knows the server contract by.
 *
 * Aliases of generated types, every one. Nothing here declares a field, a
 * scalar or a nullability, so nothing here can disagree with the schema.
 */

import type { WorkspaceAnalyticsQuery } from '../../../generated/operations'

export type { EstimateScale, WorkflowStateCategory } from '../../../generated/schema'

/** Everything one analytics page reads, for one workspace and one window. */
export type WorkspaceAnalytics = WorkspaceAnalyticsQuery['workspaceAnalytics']

/** One UTC day: what was filed on it, and what stopped on it. */
export type ThroughputDay = WorkspaceAnalytics['throughput'][number]

/** The window's three sums, and the completion rate two of them support. */
export type ThroughputTotals = WorkspaceAnalytics['totals']

/**
 * A distribution of durations in hours.
 *
 * `leadTime` and `issueAge` are both this shape. `cycleTime` deliberately is
 * NOT -- it carries its own coverage, because it is the one duration this
 * schema cannot measure for every issue.
 */
export type DurationSummary = NonNullable<WorkspaceAnalytics['leadTime']>

/** Time spent being worked on, and how much of the work that covers. */
export type CycleTimeSummary = NonNullable<WorkspaceAnalytics['cycleTime']>

export type StateCategoryCount = WorkspaceAnalytics['stateMix'][number]
export type PriorityCount = WorkspaceAnalytics['priorityMix'][number]
export type AssigneeWorkload = WorkspaceAnalytics['workload'][number]
export type TeamCompletion = WorkspaceAnalytics['teams'][number]
export type ProjectProgress = WorkspaceAnalytics['projects'][number]
export type CycleProgress = WorkspaceAnalytics['cycles'][number]
