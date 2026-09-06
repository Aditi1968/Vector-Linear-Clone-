import type { RouteObject } from 'react-router-dom'

/**
 * TEMPORARY. Delete this file when `features/onboarding` lands.
 *
 * Same reason as `features/auth/__shellStandIn.tsx`: the shell composes its
 * route table from this module and is being built alongside it.
 *
 * The contract the shell is written against:
 *
 *     onboardingRoutes: RouteObject[]      // '/onboarding'
 *
 * The shell sends a signed-in user with no workspace here -- from
 * `app/routes/WorkspaceEntry` when the URL names no workspace, and from
 * `app/layout/AppLayout` when it names one they cannot see. Both use
 * `ONBOARDING_PATH` in `app/routes/paths.ts`, which must keep matching the
 * path declared below.
 */
function OnboardingStandIn() {
  return (
    <main>
      <h1>Create your workspace</h1>
      <p>Onboarding is not wired up in this build.</p>
    </main>
  )
}

export const onboardingRoutes: RouteObject[] = [
  { path: 'onboarding', element: <OnboardingStandIn /> },
]
