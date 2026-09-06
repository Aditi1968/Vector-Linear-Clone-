import { Navigate, Outlet, useLocation, useOutletContext } from 'react-router-dom'

import { ErrorState, Spinner, VectorMark, VisuallyHidden } from '../../../components'
import { useOnboardingProgress } from '../api'
import type { OnboardingMembership, OnboardingStep } from '../lib/progress'
import {
  ONBOARDING_STEPS,
  onboardingPaths,
  resolveStep,
  stepFromPathname,
  workspaceHome,
} from '../lib/progress'
import styles from '../onboarding.module.css'

/** What every step is given. `membership` is null only on the first step. */
export interface OnboardingContext {
  membership: OnboardingMembership | null
}

/** The context the layout provides. Steps read it through this hook. */
export function useOnboardingContext(): OnboardingContext {
  return useOutletContext<OnboardingContext>()
}

const STEP_LABELS: Record<OnboardingStep, string> = {
  workspace: 'Workspace',
  team: 'Team',
  invite: 'Invite',
  integrations: 'Integrations',
}

/**
 * The frame around first-run setup, and the guard that decides which step is
 * allowed to render.
 *
 * ## Why the guard lives here and not in each step
 *
 * There is exactly one rule -- ../lib/progress.ts's `resolveStep` -- and it
 * is applied once, before any step mounts. A guard repeated per step is four
 * chances to write it slightly differently, and the copy that is wrong is
 * the one nobody visits often.
 *
 * ## Why `<Navigate>` and not an effect
 *
 * A redirect decided in `useEffect` renders the wrong step first and
 * corrects afterwards, which is a visible flash and, worse, a mounted step
 * that runs its own queries against a workspace it should never have seen.
 * Returning `<Navigate replace>` during render never mounts it at all.
 * `replace` because a corrective redirect must not sit in history --
 * otherwise Back from the team step lands on the workspace step, which
 * bounces forward again, and the browser's Back button is dead.
 *
 * ## Nothing here checks that anyone is signed in
 *
 * Deliberately, and not something this feature can fix on this branch. Every
 * query below is authenticated server-side and answers an unauthenticated
 * caller with a top-level `UNAUTHENTICATED` error rather than with data, so
 * setup cannot proceed without a session whatever the client believes -- the
 * failure is safe, it is merely ugly. The client half (sending someone to a
 * sign-in screen instead of showing an error) belongs to `<RequireAuth>` in
 * `features/auth`, which does not exist yet. `onboardingRoutes` is exported
 * as plain route data precisely so that wrapping it costs one line once it
 * does; see ../index.ts.
 */
export function OnboardingLayout() {
  const progress = useOnboardingProgress()
  const location = useLocation()

  const step = stepFromPathname(location.pathname)

  if (progress.loading) {
    return (
      <main className={styles.page}>
        <p className={styles.loading}>
          <Spinner />
          <VisuallyHidden>Checking your setup</VisuallyHidden>
        </p>
      </main>
    )
  }

  if (progress.error !== null) {
    return (
      <main className={styles.page}>
        <ErrorState
          description="Vector could not work out where you had got to."
          detail={progress.error}
          onRetry={progress.refetch}
          title="Could not load your setup"
        />
      </main>
    )
  }

  const { membership } = progress

  const resolution = resolveStep(step, {
    hasWorkspace: membership !== null,
    hasTeam: progress.hasTeam,
  })

  if (resolution !== null) {
    return (
      <Navigate
        replace
        to={
          resolution.redirectTo === 'product'
            ? // Non-null by construction: `resolveStep` answers 'product'
              // only when there is a workspace. Defaulted rather than
              // asserted, so a regression in that function is a redirect to
              // `/` -- which resolves a workspace of its own -- rather than
              // a crash in the guard every step goes through.
              workspaceHome(membership?.workspace.slug ?? '')
            : onboardingPaths.step(resolution.redirectTo)
        }
      />
    )
  }

  // Unreachable: `resolveStep` always redirects a null step. Narrowed rather
  // than asserted, for the same reason as above.
  if (step === null) {
    return null
  }

  const position = ONBOARDING_STEPS.indexOf(step) + 1

  return (
    <main className={styles.page}>
      <div className={styles.frame}>
        <p className={styles.brand}>
          <VectorMark />
          <span>Vector</span>
        </p>

        {/*
         * Step progress, conveyed rather than merely drawn.
         *
         * An ordered list, so the count and each item's position are in the
         * accessibility tree for free; `aria-current="step"` on the one being
         * shown, which is the attribute value that exists for exactly this;
         * and a visually-hidden "Step 2 of 4", because numbered dots do not
         * say how far along this is to anyone who cannot see them.
         *
         * Not a `<nav>` of links: the steps are not navigable -- the guard
         * decides which one is reachable -- and rendering them as links that
         * silently redirect would be lying about what they do.
         */}
        <ol aria-label="Setup steps" className={styles.steps}>
          {ONBOARDING_STEPS.map((candidate, index) => (
            <li
              aria-current={candidate === step ? 'step' : undefined}
              className={styles.stepItem}
              data-state={
                candidate === step
                  ? 'current'
                  : index < position - 1
                    ? 'done'
                    : 'upcoming'
              }
              key={candidate}
            >
              <span aria-hidden="true" className={styles.stepMarker}>
                {index + 1}
              </span>
              {STEP_LABELS[candidate]}
            </li>
          ))}
        </ol>

        <VisuallyHidden>
          <p>{`Step ${position} of ${ONBOARDING_STEPS.length}: ${STEP_LABELS[step]}`}</p>
        </VisuallyHidden>

        <Outlet context={{ membership } satisfies OnboardingContext} />
      </div>
    </main>
  )
}
