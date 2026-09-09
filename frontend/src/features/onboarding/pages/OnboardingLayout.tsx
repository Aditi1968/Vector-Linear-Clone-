import { Navigate, Outlet, useLocation, useOutletContext } from 'react-router-dom'

import {
  CheckIcon,
  ErrorState,
  Spinner,
  VectorMark,
  VisuallyHidden,
} from '../../../components'
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
 * `01`, not `1`.
 *
 * The scale down the rail is read as a column of numerals, and a column that
 * alternates between one and two glyphs is not a scale, it is a list that
 * happens to be numbered. Zero-padding is a drawing decision and never
 * reaches a screen reader: every one of these is `aria-hidden`, and the
 * position is announced by the `<ol>` and by the sentence below it.
 */
function pad(value: number): string {
  return String(value).padStart(2, '0')
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
    <div className={styles.shell}>
      {/*
       * The rail: brand at the top, the scale in the middle, the counter at
       * the foot. Not a `<nav>` and not an `<aside>` -- there is nothing to
       * navigate to and nothing complementary about it. It is a progress
       * readout, and the `<ol>` inside it is the part that carries meaning.
       */}
      <div className={styles.rail}>
        <p className={styles.brand}>
          <VectorMark className={styles.brandMark} />
          <span>Vector</span>
        </p>

        {/*
         * Progress as a calibrated scale, read top to bottom.
         *
         * An ordered list, so the count and each item's position are in the
         * accessibility tree for free; `aria-current="step"` on the one being
         * shown, which is the attribute value that exists for exactly this;
         * and a visually-hidden "Step 2 of 4" below, because a numeral and a
         * dot do not say how far along this is to anyone who cannot see them.
         *
         * The tick on a finished step is decoration with a word behind it.
         * Green and a check mark are two ways of saying the same thing and
         * both are visual, so the state that a sighted reader gets from the
         * glyph is spelled out for everyone else -- otherwise "done" is
         * carried by colour alone, which is the failure this codebase draws
         * different silhouettes per status category to avoid.
         *
         * Not a list of links: the steps are not navigable -- the guard above
         * decides which one is reachable -- and rendering them as links that
         * silently redirect would be lying about what they do.
         */}
        <ol aria-label="Setup steps" className={styles.steps}>
          {ONBOARDING_STEPS.map((candidate, index) => {
            const state =
              candidate === step
                ? 'current'
                : index < position - 1
                  ? 'done'
                  : 'upcoming'

            return (
              <li
                aria-current={candidate === step ? 'step' : undefined}
                className={styles.stepItem}
                data-state={state}
                key={candidate}
              >
                <span aria-hidden="true" className={styles.stepIndex}>
                  {pad(index + 1)}
                </span>
                <span className={styles.stepLabel}>{STEP_LABELS[candidate]}</span>
                <span className={styles.stepMark}>
                  {state === 'done' && (
                    <>
                      <CheckIcon className={styles.stepTick} />
                      <VisuallyHidden>Completed</VisuallyHidden>
                    </>
                  )}
                  {state === 'current' && (
                    <span aria-hidden="true" className={styles.stepDot} />
                  )}
                </span>
              </li>
            )
          })}
        </ol>

        <VisuallyHidden>
          <p>{`Step ${position} of ${ONBOARDING_STEPS.length}: ${STEP_LABELS[step]}`}</p>
        </VisuallyHidden>

        {/*
         * The same fact as the sentence above it, drawn. `aria-hidden`, so it
         * is not announced twice -- and because "Step 01 / 04" is a legend on
         * an instrument, not a sentence anyone wants read aloud.
         */}
        <p aria-hidden="true" className={styles.counter}>
          {`Step ${pad(position)} / ${pad(ONBOARDING_STEPS.length)}`}
        </p>
      </div>

      <main className={styles.content}>
        <div className={styles.frame}>
          <Outlet context={{ membership } satisfies OnboardingContext} />
        </div>
      </main>
    </div>
  )
}
