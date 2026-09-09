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
 * The fragment's five fields -- `id`, `identifier`, `title`, `description`,
 * `priority` -- and that is the single most important fact about this screen.
 *
 * Note *the fragment's*, not the type's: `IssueSummary` has been widened and
 * now carries `workflowStateId`, `assigneeId`, `dueDate` and more. This alias
 * follows the selection set, so it still has none of them, and TypeScript is
 * the guard -- reaching for `issue.assigneeId` here does not compile. A row
 * draws what is here and nothing else, because a cell filled from a field the
 * query never asked for is a lie the row cannot detect.
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
