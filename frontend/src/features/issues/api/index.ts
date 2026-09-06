/**
 * The issues data adapter.
 *
 * This is the boundary the rest of the feature is written against. Screens
 * and rows import hooks and types from here; they never import `gql`, a
 * document, an Apollo hook, or an Apollo error type. Backend Phase 1b-5 makes
 * issue operations workspace-aware -- new arguments, new variables, quite
 * possibly a new cache key -- and the value of that rule is that the change
 * lands in this directory and stops here.
 *
 * The documents are exported too, and only from this module rather than from
 * the feature root, because mocking a GraphQL response requires the exact
 * document that produced it. That is a legitimate need of a test file and not
 * a licence for a component to run its own query.
 */

export { useIssueList } from './useIssueList'
export type { UseIssueListResult } from './useIssueList'

export { useIssueDetail } from './useIssueDetail'
export type { UseIssueDetailResult } from './useIssueDetail'

export { useCreateIssue } from './useCreateIssue'
export type { CreateIssueOutcome, UseCreateIssueResult } from './useCreateIssue'

export { prependCreatedIssue } from './cache'

export {
  IssueCreateDocument,
  IssueDetailDocument,
  IssueListDocument,
  WorkspaceTeamsDocument,
} from './documents'

export type {
  IssueConnection,
  IssueCreateData,
  IssueCreateInput,
  IssueCreatePayload,
  IssueCreateVariables,
  IssueDetailData,
  IssueDetailFields,
  IssueDetailVariables,
  IssueDraft,
  IssueListData,
  IssueListVariables,
  IssuePageInfo,
  IssueRowFields,
  IssueValidationError,
  WorkspaceTeam,
  WorkspaceTeamsData,
  WorkspaceTeamsVariables,
} from './types'
