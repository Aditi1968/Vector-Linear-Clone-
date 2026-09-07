/**
 * Every GraphQL document this feature sends.
 *
 * Written in ./operations.graphql and compiled into `TypedDocumentNode`s by
 * `npm run graphql:codegen`. This module is the seam between the generated
 * output and the feature, so a change of generated layout lands on one import
 * rather than on every hook. Nothing outside `./` imports from
 * `src/generated`, and no presentational component imports a document at all.
 */

export {
  TriageAcceptDocument,
  TriageChangeTeamDocument,
  TriageDeclineDocument,
  TriageEnterDocument,
  TriageIssueUpdateDocument,
  TriageMarkDuplicateDocument,
  TriageQueueDocument,
} from '../../../generated/operations'
