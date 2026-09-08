/**
 * Every GraphQL document this feature sends.
 *
 * Written in ./operations.graphql and compiled into `TypedDocumentNode`s by
 * `npm run graphql:codegen`. This module is the seam between the generated
 * output and the feature.
 *
 * The module name collides with the domain noun, which is unfortunate and
 * kept anyway: every feature in this codebase has an `api/documents.ts`
 * meaning "the GraphQL documents", and renaming this one would make the
 * documents feature the exception nobody looks for.
 */

export {
  DocumentCommentCreateDocument,
  DocumentCommentDeleteDocument,
  DocumentCreateDocument,
  DocumentDeleteDocument,
  DocumentDetailDocument,
  DocumentEditDocument,
  DocumentListDocument,
  DocumentRestoreDocument,
} from '../../../generated/operations'
