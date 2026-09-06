/**
 * The cycles data adapter.
 *
 * The boundary the rest of the feature is written against. Screens import
 * hooks and types from here and never a document, an Apollo hook, or an
 * Apollo error type. The documents are exported too, and only from this
 * module, because mocking a response requires the exact document that
 * produced it -- a need of a test file, not a licence for a component to run
 * its own query.
 */

export { useCycleDetail, useCycleIssues, useCycleList, useCycleTeams } from './queries'
export type {
  UseCycleDetailResult,
  UseCycleIssuesResult,
  UseCycleListResult,
  UseCycleTeamsResult,
} from './queries'

export { useCycleActions } from './mutations'
export type { CycleOutcome, UseCycleActionsResult } from './mutations'

export { describeError } from './errors'

export {
  CycleCreateDocument,
  CycleDeleteDocument,
  CycleDetailDocument,
  CycleIssuesDocument,
  CycleListDocument,
  CycleTeamsDocument,
  CycleUpdateDocument,
  IssueSetCycleDocument,
} from './documents'

export type {
  CycleCreateInput,
  CycleDraft,
  CycleFields,
  CycleIssue,
  CycleTeam,
  CycleUpdateFields,
  CycleUpdateInput,
  CycleValidationError,
} from './types'
