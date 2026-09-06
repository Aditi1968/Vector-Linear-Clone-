/**
 * Every GraphQL document this feature sends.
 *
 * Written in ./operations.graphql and compiled into `TypedDocumentNode`s by
 * `npm run graphql:codegen`. This module is the seam between the generated
 * output and the feature, so a change of generated layout -- a different
 * directory, a different naming convention -- is one import to fix rather
 * than one per panel.
 *
 * Nothing outside `./` imports a document from `src/generated`, and no panel
 * imports a document at all: panels consume the hooks beside this file, and
 * the hooks consume these.
 */

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
} from '../../../generated/operations'
