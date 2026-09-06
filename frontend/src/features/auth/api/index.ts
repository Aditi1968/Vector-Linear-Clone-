/**
 * The auth data adapter.
 *
 * The boundary the rest of the feature -- and the rest of the application --
 * is written against. Screens import hooks from here; they never import a
 * document, an Apollo hook or an Apollo error type.
 *
 * The documents are exported from this module and not from the feature root,
 * because mocking a response requires the exact document that produced it.
 * That is a legitimate need of a test file and of the session-expiry link,
 * not a licence for a component to run its own query.
 */

export { useViewer } from './useViewer'
export type { UseViewerResult } from './useViewer'

export { useLogin, useRegister } from './useSignIn'
export type {
  LoginFields,
  RegisterFields,
  SignInOutcome,
  UseSignInResult,
} from './useSignIn'

export { useLogout } from './useLogout'
export type { UseLogoutResult } from './useLogout'

export { useSignedInDestination } from './useSignedInDestination'
export type { UseSignedInDestinationResult } from './useSignedInDestination'

export { ONBOARDING_PATH, signedInDestination } from './destination'

export { sessionExpiryLink } from './sessionExpiry'

export {
  LoginDocument,
  LogoutDocument,
  MeDocument,
  MyWorkspacesDocument,
  RegisterDocument,
} from './documents'

export type { AuthValidationError, Viewer, ViewerMembership } from './types'
