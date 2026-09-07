/**
 * The names this feature knows the server contract by.
 *
 * Aliases of generated types, every one. Nothing here declares a field, a
 * scalar or a nullability, so nothing here can disagree with the schema:
 * change ./operations.graphql, regenerate, and these follow.
 */

import type {
  TriageAcceptMutation,
  TriageQueueQuery,
  TriageRowFieldsFragment,
} from '../../../generated/operations'

/** One row of the queue: an issue summary and when it arrived. */
export type TriageRow = TriageRowFieldsFragment

/**
 * The issue on a row.
 *
 * An `IssueSummary`, and that is the single most important fact about this
 * screen. It has `id`, `identifier`, `title`, `description` and `priority`
 * and it does NOT have `assigneeId`, `workflowStateId`, `labels` or
 * `dueDate` -- see ./operations.graphql for why resolving those per row is
 * not on the table. A row draws what is here and nothing else.
 */
export type TriageIssue = TriageRow['issue']

/** The queue connection, for the hook that pages through it. */
export type TriageConnection = TriageQueueQuery['triageIssues']

/**
 * One entry of a payload's `errors`.
 *
 * Not GraphQL errors: these arrive inside `data` over a successful response
 * and describe input the server refused -- accepting into a state that is
 * not that team's, marking an issue a duplicate of itself.
 */
export type TriageValidationError = TriageAcceptMutation['triageAccept']['errors'][number]
