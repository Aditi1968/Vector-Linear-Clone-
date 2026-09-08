/**
 * Every GraphQL document this feature sends.
 *
 * Written in ./operations.graphql and compiled into `TypedDocumentNode`s by
 * `npm run graphql:codegen`. This module is the seam between the generated
 * output and the feature.
 */

export {
  GroupedLabelListDocument,
  LabelGroupCreateDocument,
  LabelGroupDeleteDocument,
  LabelGroupListDocument,
  LabelGroupUpdateDocument,
  LabelSetGroupDocument,
} from '../../../generated/operations'
