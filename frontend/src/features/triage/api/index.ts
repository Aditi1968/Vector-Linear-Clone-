/**
 * The triage data adapter.
 *
 * The boundary the rest of the feature is written against. The screen imports
 * hooks and types from here and never a document, an Apollo hook, or an
 * Apollo error type. The documents are exported too, and only from this
 * module, because mocking a response requires the exact document that
 * produced it -- a need of a test file, not a licence for a component to run
 * its own query.
 *
 * ## One operation this feature does not use
 *
 * `triageEnter` is compiled (see ./operations.graphql) and deliberately not
 * wired to a control. It puts an issue INTO the queue, and every issue this
 * screen can see is in the queue already -- so the affordance belongs on an
 * issue's own detail view, which is `features/issues`' to own, not here. A
 * button that could only ever act on rows absent from the page would be
 * scaffolding, so the document is left ready and unused rather than given a
 * home it does not have.
 */

export { useTriageQueue } from './queries'
export type { UseTriageQueueResult } from './queries'

export { useTriageActions } from './mutations'
export type { TriageOutcome, UseTriageActionsResult } from './mutations'

export {
  TriageAcceptDocument,
  TriageChangeTeamDocument,
  TriageDeclineDocument,
  TriageEnterDocument,
  TriageIssueUpdateDocument,
  TriageMarkDuplicateDocument,
  TriageQueueDocument,
} from './documents'

export type {
  TriageConnection,
  TriageIssue,
  TriageRow,
  TriageValidationError,
} from './types'
