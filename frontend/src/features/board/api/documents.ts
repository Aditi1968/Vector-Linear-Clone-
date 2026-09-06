/**
 * The two documents the board sends.
 *
 * Written in ./operations.graphql and compiled by `npm run graphql:codegen`.
 * This module is the seam between the generated output and the feature, so
 * that a change of generated layout is one import to fix rather than a sweep;
 * it follows `features/issues/api/documents.ts`, which explains the rule at
 * length.
 */

export {
  BoardIssueMoveDocument,
  BoardIssuesDocument,
} from '../../../generated/operations'
