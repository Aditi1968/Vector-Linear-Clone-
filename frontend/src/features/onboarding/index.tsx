import type { RouteObject } from 'react-router-dom'

import { AcceptInvitePage } from './pages/AcceptInvitePage'
import { IntegrationsStep } from './pages/IntegrationsStep'
import { InviteStep } from './pages/InviteStep'
import { OnboardingLayout } from './pages/OnboardingLayout'
import { TeamStep } from './pages/TeamStep'
import { WorkspaceStep } from './pages/WorkspaceStep'
import {
  INVITE_ROOT,
  INVITE_TOKEN_PARAM,
  ONBOARDING_ROOT,
  ONBOARDING_STEPS,
} from './lib/progress'

/**
 * First-run setup: the path from a new account to a usable workspace.
 *
 * ## Wiring, for whoever owns `src/app/routes/routes.tsx`
 *
 *     import { onboardingRoutes } from '../../features/onboarding'
 *
 *     export const routes: RouteObject[] = [
 *       ...onboardingRoutes,
 *       { index: true, element: <WorkspaceEntry /> },
 *       { path: `/:${WORKSPACE_SLUG_PARAM}`, ... },
 *     ]
 *
 * Order does not matter: React Router 7 ranks by specificity, not by
 * position, and a static `onboarding` segment outranks the dynamic
 * `:workspaceSlug` it would otherwise be matched as. Spreading rather than
 * nesting because these are top-level routes -- they render outside
 * `AppLayout`, whose sidebar links are built from a workspace slug that does
 * not exist yet.
 *
 * Wrapping for authentication is a one-line change here when
 * `features/auth` lands:
 *
 *     { element: <RequireAuth />, children: onboardingRoutes }
 *
 * ...for the `/onboarding` entry. `/invite/:token` should NOT be wrapped
 * blindly: it is the URL a signed-out person clicks out of a chat message,
 * so it wants "sign in, then come back here", not "sign in, then land
 * somewhere else". Until that exists the page reports the server's
 * `UNAUTHENTICATED` refusal rather than pretending to work.
 *
 * ## One thing the Lead has to connect
 *
 * `src/app/routes/WorkspaceEntry.tsx` renders "No workspace yet" for an
 * account with no memberships. That is exactly the person this feature
 * exists for, and it should be `<Navigate to="/onboarding" replace />`
 * instead. That file is not mine to edit.
 */
export const onboardingRoutes: RouteObject[] = [
  {
    path: ONBOARDING_ROOT,
    element: <OnboardingLayout />,
    children: [
      {
        /*
         * `/onboarding` itself is a router, not a page.
         *
         * The layout resolves it: to whatever step is unfinished, or into
         * the product when nothing is -- which is the rule that an existing
         * user landing here is never shown setup a second time. The index
         * element is `null` because the layout has already redirected by the
         * time an outlet would render.
         */
        index: true,
        element: null,
      },
      // Declared from the same array the progress indicator and the state
      // machine read, so a step cannot exist in one and not the other.
      { path: ONBOARDING_STEPS[0], element: <WorkspaceStep /> },
      { path: ONBOARDING_STEPS[1], element: <TeamStep /> },
      { path: ONBOARDING_STEPS[2], element: <InviteStep /> },
      { path: ONBOARDING_STEPS[3], element: <IntegrationsStep /> },
    ],
  },
  {
    // Outside the onboarding layout: someone redeeming an invitation is not
    // being set up, they are joining something already set up, and the step
    // indicator would be describing a flow they are not in.
    path: `${INVITE_ROOT}/:${INVITE_TOKEN_PARAM}`,
    element: <AcceptInvitePage />,
  },
]
