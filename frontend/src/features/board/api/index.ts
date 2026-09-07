/**
 * The board's data adapter.
 *
 * Components consume these hooks; they never import `gql`, a document, or an
 * Apollo hook. The documents are exported too, and only from here, because
 * mocking a response in a test requires the exact document that produced it.
 *
 * There is no `types.ts` beside this file, and that is not an omission. A
 * board card is an `IssueRowFields` -- the same fragment the issue list
 * selects -- so the board has no shape of its own to name, and an alias would
 * be a second name for one type rather than a boundary.
 */

export { useBoardIssues } from './useBoardIssues'
export type { UseBoardIssuesResult } from './useBoardIssues'

export { useBoardLabels } from './useBoardLabels'
export type { BoardLabel } from './useBoardLabels'

export { useMoveIssue } from './useMoveIssue'
export type { UseMoveIssueResult } from './useMoveIssue'

export {
  BoardIssueMoveDocument,
  BoardIssuesDocument,
  BoardLabelsDocument,
} from './documents'
