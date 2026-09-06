/*
 * Imported from the path module directly rather than through
 * `app/routes/index.ts`, on purpose: that barrel re-exports the route table,
 * which will import this feature's `authRoutes`, and the cycle that closes is
 * the kind that resolves to `undefined` at module-evaluation time rather than
 * failing loudly. `paths.ts` itself imports nothing.
 */
import { createAppPaths } from '../../../app/routes/paths'
import type { ViewerMembership } from './types'

/**
 * Where a signed-in user with no workspace goes.
 *
 * A literal because the route belongs to the onboarding feature, which
 * exports its routes as data and no path builder. When it publishes one this
 * becomes a call to it; until then this is the single place the string
 * appears.
 */
export const ONBOARDING_PATH = '/onboarding'

/**
 * Where signing in should land somebody.
 *
 * Derived from what the server says they are a member of, never assumed. A
 * fresh account has no workspace and its issue list would be an empty screen
 * with no way forward, so it goes to onboarding instead; anybody with a
 * membership goes to a workspace.
 *
 * The first membership, because the server returns them in a stable order and
 * a "last used workspace" preference would need somewhere to live -- which,
 * for a signed-out visitor about to sign in, would be local storage. Not
 * worth it for a choice that only matters to people in several workspaces.
 *
 * The workspace URL is built through `createAppPaths` with the slug as the
 * scope prefix, which is exactly the shape `paths.ts` documents for when
 * routes gain a `/:workspaceSlug` segment. Sending someone to a scoped URL
 * before the route table declares one is the intended direction of travel,
 * and it is one call site to revisit rather than a convention to remember.
 */
export function signedInDestination(memberships: readonly ViewerMembership[]): string {
  const first = memberships[0]

  if (first === undefined) {
    return ONBOARDING_PATH
  }

  // The bare slug. `createAppPaths` takes the workspace, not a URL prefix,
  // and encodes the segment itself -- handing it a pre-built `/slug` yields
  // `/%2Fslug`, which is a real workspace nobody has.
  return createAppPaths(first.workspace.slug).issues()
}
