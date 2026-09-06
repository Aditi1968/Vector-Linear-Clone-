/**
 * Every GraphQL document this feature sends.
 *
 * The documents themselves are written in ./operations.graphql and compiled
 * into `TypedDocumentNode`s by `npm run graphql:codegen`. This module is the
 * seam between the generated output and the feature: it is what makes
 * "where do this feature's documents come from" a one-file answer, and what
 * confines a change of generated layout -- a different directory, a
 * different naming convention, a preset -- to a single import.
 *
 * Nothing outside `./` imports a document from `src/generated`, and no
 * presentational component imports a document at all. Components consume the
 * hooks in this directory, which consume these.
 *
 * ## What a `TypedDocumentNode` buys here
 *
 * Each of these carries its own result type and variables type as type
 * parameters, so `useQuery(IssueListDocument)` infers both ends with no
 * annotation and no cast at the call site. A field added to
 * ./operations.graphql and not regenerated is a failing `npm run
 * graphql:check`; a field *read* that the document does not select is a
 * compile error at the component that reads it.
 */

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
} from '../../../generated/operations'
