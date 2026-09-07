/**
 * Every GraphQL document this feature sends.
 *
 * Written in ./operations.graphql and compiled into `TypedDocumentNode`s by
 * `npm run graphql:codegen`. This module is the seam between the generated
 * output and the feature, so a change of generated layout lands on one import
 * rather than on every hook.
 */

export {
  SavedViewCreateDocument,
  SavedViewDeleteDocument,
  SavedViewListDocument,
  SavedViewResultsDocument,
  SavedViewUpdateDocument,
} from '../../../generated/operations'
