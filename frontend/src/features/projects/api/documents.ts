/**
 * Every GraphQL document this feature sends.
 *
 * Written in ./operations.graphql and compiled into `TypedDocumentNode`s by
 * `npm run graphql:codegen`. This module is the seam between the generated
 * output and the feature, so a change of generated layout -- a different
 * directory, a preset, a naming convention -- lands on one import rather
 * than on every hook.
 *
 * Nothing outside `./` imports from `src/generated`, and no presentational
 * component imports a document at all: components use the hooks beside this
 * file, and tests import documents from `../api` because mocking a response
 * requires the exact document that produced it.
 */

export {
  IssueSetProjectDocument,
  ProjectCreateDocument,
  ProjectDeleteDocument,
  ProjectDetailDocument,
  ProjectIssuesDocument,
  ProjectListDocument,
  ProjectMembersDocument,
  ProjectMilestoneCreateDocument,
  ProjectMilestoneDeleteDocument,
  ProjectMilestoneUpdateDocument,
  ProjectTeamAddDocument,
  ProjectTeamRemoveDocument,
  ProjectTeamsDocument,
  ProjectUpdateDocument,
} from '../../../generated/operations'
