/**
 * The seam between codegen's output and this feature.
 *
 * The documents are written in ./operations.graphql and compiled by
 * `npm run graphql:codegen`. Nothing outside `./` imports one from
 * `src/generated`, and no page imports one at all -- pages consume the hooks
 * beside this file. Same rule as `features/issues/api/documents.ts`, and it
 * pays for itself the same way: agent A0 is adding a required
 * `workspaceSlug` argument to several root fields, and a change of that kind
 * lands in this directory and stops here.
 */

export {
  OnboardingIntegrationsDocument,
  OnboardingInvitationAcceptDocument,
  OnboardingInvitationCreateDocument,
  OnboardingTeamCreateDocument,
  OnboardingTeamsDocument,
  OnboardingWorkspaceCreateDocument,
  OnboardingWorkspacesDocument,
} from '../../../generated/operations'
