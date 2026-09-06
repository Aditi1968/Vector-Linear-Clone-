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
 * hooks in this directory (./useIssueList, ./useIssueDetail,
 * ./useCreateIssue), which consume these. That is not layering for its own
 * sake: backend Phase 1b-5 makes issue operations workspace-aware, which
 * changes these documents' arguments and variables. Confining that to this
 * directory is what keeps the change from reaching every screen that renders
 * an issue.
 *
 * ## What a `TypedDocumentNode` buys here
 *
 * Each of these carries its own result type and variables type as type
 * parameters, so `useQuery(IssueListDocument)` infers both ends with no
 * annotation and no cast at the call site. A field added to
 * ./operations.graphql and not regenerated is a failing `npm run
 * graphql:check`; a field *read* that the document does not select is a
 * compile error at the component that reads it.
 *
 * ## Why `gql` no longer appears anywhere in this feature
 *
 * The documents used to be `gql` template literals here, with the page size
 * interpolated from `DEFAULT_PAGE_SIZE`. Moving them to a `.graphql` file
 * cost that interpolation -- a GraphQL document cannot reference a
 * TypeScript constant -- so the page size is now a literal `25` in the
 * document with ./documents.test.ts asserting it equals `DEFAULT_PAGE_SIZE`.
 * What it bought is that the operation text is now checked against the real
 * schema at generation time rather than at request time: a misspelled field
 * or a wrong variable type fails `npm run graphql:codegen`, where before it
 * failed as a server-side validation error in the browser.
 */

export {
  IssueCreateDocument,
  IssueDetailDocument,
  IssueListDocument,
} from '../../../generated/operations'
