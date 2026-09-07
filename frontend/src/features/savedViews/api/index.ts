/**
 * The saved-views data adapter.
 *
 * The boundary the rest of the feature is written against. Screens import
 * hooks and types from here and never a document, an Apollo hook, or an
 * Apollo error type. The documents are exported too, and only from this
 * module, because mocking a response requires the exact document that
 * produced it -- a need of a test file, not a licence for a component to run
 * its own query.
 */

export { useSavedViewList, useSavedViewResults } from './queries'
export type { UseSavedViewListResult, UseSavedViewResultsResult } from './queries'

export { useSavedViewActions } from './mutations'
export type {
  SavedViewOutcome,
  SavedViewPatch,
  UseSavedViewActionsResult,
} from './mutations'

export {
  SavedViewCreateDocument,
  SavedViewDeleteDocument,
  SavedViewListDocument,
  SavedViewResultsDocument,
  SavedViewUpdateDocument,
} from './documents'

export type {
  IssueFilterInput,
  IssueOrderField,
  OrderDirection,
  SavedViewDraft,
  SavedViewFields,
  SavedViewFilter,
  SavedViewFilterDraft,
  SavedViewGrouping,
  SavedViewIssue,
  SavedViewLayout,
  SavedViewValidationError,
  SavedViewVisibility,
  WorkflowStateCategory,
} from './types'
