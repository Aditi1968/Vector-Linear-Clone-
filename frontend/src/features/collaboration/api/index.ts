/**
 * The collaboration data adapter.
 *
 * The boundary the panels are written against. They import hooks and types
 * from here; they never import `gql`, a document, an Apollo hook, or an
 * Apollo error type.
 *
 * The documents are exported too, and only from this module rather than from
 * the feature root, because mocking a GraphQL response requires the exact
 * document that produced it. That is a legitimate need of a test file and not
 * a licence for a panel to run its own query.
 */

export { useComments } from './useComments'
export type { UseCommentsResult } from './useComments'

export { useLabels } from './useLabels'
export type { UseLabelsResult } from './useLabels'

export { useRelations } from './useRelations'
export type { UseRelationsResult } from './useRelations'

export { useSubIssues } from './useSubIssues'
export type { UseSubIssuesResult } from './useSubIssues'

export { MIN_QUERY_LENGTH, useIssueSearch } from './useIssueSearch'
export type { UseIssueSearchResult } from './useIssueSearch'

export { describeOutcome } from './outcome'
export type { MutationOutcome } from './outcome'

export {
  appendComment,
  insertChild,
  insertWorkspaceLabel,
  prependRelation,
  removeChild,
  removeComment,
  removeRelation,
} from './cache'

export {
  CommentAuthorsDocument,
  CommentCreateDocument,
  CommentDeleteDocument,
  IssueClearParentDocument,
  IssueCommentsDocument,
  IssueLabelAttachDocument,
  IssueLabelDetachDocument,
  IssueLabelsDocument,
  IssueRelationCreateDocument,
  IssueRelationDeleteDocument,
  IssueRelationsDocument,
  IssueSearchDocument,
  IssueSetParentDocument,
  IssueSubIssuesDocument,
  LabelCreateDocument,
  WorkspaceLabelsDocument,
} from './documents'

export type {
  Comment,
  CommentConnection,
  ChildConnection,
  ConnectionPageInfo,
  IssueRelation,
  IssueRelationType,
  IssueSearchHit,
  IssueSummary,
  Label,
  LabelConnection,
  RelationConnection,
  ValidationError,
  WorkspaceMember,
} from './types'
