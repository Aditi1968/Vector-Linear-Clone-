/**
 * The onboarding data adapter.
 *
 * Pages are written against this and never against `gql`, a document, an
 * Apollo hook or an Apollo error type. The documents are exported too, and
 * only from here rather than from the feature root, because mocking a
 * response requires the exact document that produced it -- a legitimate need
 * of a test file, not a licence for a page to run its own query.
 */

export { useOnboardingProgress } from './useOnboardingProgress'
export type { OnboardingProgress } from './useOnboardingProgress'

export {
  useAcceptInvitation,
  useCreateInvitation,
  useCreateTeam,
  useCreateWorkspace,
} from './mutations'
export type { CreatedInvitation, Outcome } from './mutations'

export {
  OnboardingIntegrationsDocument,
  OnboardingInvitationAcceptDocument,
  OnboardingInvitationCreateDocument,
  OnboardingTeamCreateDocument,
  OnboardingTeamsDocument,
  OnboardingWorkspaceCreateDocument,
  OnboardingWorkspacesDocument,
} from './documents'
