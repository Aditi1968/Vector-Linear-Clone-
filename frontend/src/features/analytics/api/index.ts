/**
 * The analytics data adapter.
 *
 * The boundary the rest of the feature is written against. There are no
 * mutations here and there is no `readPayload` call, because nothing on this
 * screen writes: `src/lib/graphql/payload.ts` reads a mutation's
 * `{ thing, errors }` envelope, and a query has no such envelope to read.
 */

export {
  DEFAULT_RANGE,
  MAX_RANGE_DAYS,
  RANGE_OPTIONS,
  useWorkspaceAnalytics,
} from './queries'
export type { RangeDays, UseWorkspaceAnalyticsResult } from './queries'

export { WorkspaceAnalyticsDocument } from './documents'

export type {
  AssigneeWorkload,
  CycleProgress,
  CycleTimeSummary,
  DurationSummary,
  EstimateScale,
  PriorityCount,
  ProjectProgress,
  StateCategoryCount,
  TeamCompletion,
  ThroughputDay,
  ThroughputTotals,
  WorkflowStateCategory,
  WorkspaceAnalytics,
} from './types'
