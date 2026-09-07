/**
 * The issues data adapter.
 *
 * This is the boundary the rest of the feature is written against. Screens
 * and rows import hooks and types from here; they never import `gql`, a
 * document, an Apollo hook, or an Apollo error type.
 *
 * The documents are exported too, and only from this module rather than from
 * the feature root, because mocking a GraphQL response requires the exact
 * document that produced it. That is a legitimate need of a test file and not
 * a licence for a component to run its own query.
 */

export { useIssueList } from './useIssueList'
export type { UseIssueListOptions, UseIssueListResult } from './useIssueList'

export { useIssueDetail } from './useIssueDetail'
export type { UseIssueDetailResult } from './useIssueDetail'

export { useCreateIssue } from './useCreateIssue'
export type { UseCreateIssueResult } from './useCreateIssue'

export { useIssueMutations } from './useIssueMutations'
export type { UseIssueMutationsResult } from './useIssueMutations'

export { memberLabel, useWorkspaceContext } from './useWorkspaceContext'
export type { WorkspaceContext } from './useWorkspaceContext'

export { useTeamCycles } from './useTeamCycles'

export { groupByField } from './outcome'
export type { IssueSaveOutcome } from './outcome'

export { prependCreatedIssue, removeArchivedIssue } from './cache'

export {
  IssueArchiveDocument,
  IssueCreateDocument,
  IssueDetailDocument,
  IssueListDocument,
  IssueSetCycleDocument,
  IssueSetProjectDocument,
  IssueUpdateDocument,
  IssueWorkspaceContextDocument,
  TeamCyclesDocument,
} from './documents'

export type {
  IssueArchiveData,
  IssueArchiveVariables,
  IssueConnection,
  IssueCreateData,
  IssueCreateInput,
  IssueCreatePayload,
  IssueCreateVariables,
  IssueCycle,
  IssueDetailData,
  IssueDetailFields,
  IssueDetailVariables,
  IssueDraft,
  IssueFilterInput,
  IssueLabel,
  IssueListData,
  IssueListVariables,
  IssueOrderInput,
  IssuePageInfo,
  IssuePatch,
  IssueProject,
  IssueRowFields,
  IssueSetCycleData,
  IssueSetProjectData,
  IssueUpdateData,
  IssueUpdateVariables,
  IssueValidationError,
  IssueWorkspaceContextData,
  IssueWorkspaceContextVariables,
  TeamCycle,
  TeamCyclesData,
  TeamCyclesVariables,
  WorkflowState,
  WorkflowStateCategory,
  WorkspaceMember,
  WorkspaceProject,
  WorkspaceTeam,
} from './types'
