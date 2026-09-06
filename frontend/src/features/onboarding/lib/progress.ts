/**
 * The onboarding state machine, as pure functions.
 *
 * Separated from the hook that fetches (../api/useOnboardingProgress) so the
 * rules can be read and tested without a router or an Apollo client. There
 * are only two facts and one function, and all three are below.
 */

import { createAppPaths } from '../../../app/routes'
import type { OnboardingWorkspacesQuery } from '../../../generated/operations'

/** One membership as `OnboardingWorkspaces` selects it. */
export type OnboardingMembership = OnboardingWorkspacesQuery['myWorkspaces'][number]

/** The steps, in the order they are offered. */
export const ONBOARDING_STEPS = [
  'workspace',
  'team',
  'invite',
  'integrations',
] as const

export type OnboardingStep = (typeof ONBOARDING_STEPS)[number]

/**
 * The two facts the whole machine is made of.
 *
 * `hasWorkspace` is `myWorkspaces` being non-empty. `hasTeam` is
 * `teams(workspaceSlug:)` being non-empty. Nothing else is consulted and
 * nothing is stored: there is no `onboarding_step` column, no localStorage
 * key and no wizard cursor anywhere in this feature.
 *
 * That is what makes a refresh mid-setup resume rather than restart, and it
 * is not merely a nicety -- a stored cursor is a second source of truth, and
 * it disagrees with the first at exactly the worst moment, when a step has
 * just been completed and the write recording that fails. Derived state
 * cannot be wrong about work that actually happened.
 *
 * Note what is *absent*: a fact for the invite step or the integrations
 * step. Neither has one, because neither can be unfinished -- a workspace
 * with no invitations and no GitHub connection is a finished workspace. A
 * step that could never be satisfied would trap whoever skipped it in a
 * wizard with no exit.
 */
export interface OnboardingFacts {
  hasWorkspace: boolean
  hasTeam: boolean
}

/**
 * Which workspace the rest of onboarding is about.
 *
 * The oldest membership, not `myWorkspaces[0]`. The server promises no
 * order, and "whichever came back first" is the kind of dependency that
 * works until a query plan changes. Oldest is also the right answer twice
 * over: a brand-new account has exactly one workspace so the choice is
 * vacuous, and a returning user's oldest workspace is their primary one, so
 * `/onboarding` sends them somewhere stable rather than somewhere that
 * depends on which workspace they most recently joined.
 *
 * The known limit: someone who belongs to an established workspace and then
 * accepts an invitation to an empty one is judged on the established one and
 * is correctly treated as set up. That empty workspace's first team gets
 * created from inside the product, which is where team creation lives
 * anyway.
 */
export function selectOnboardingWorkspace(
  memberships: readonly OnboardingMembership[] | undefined,
): OnboardingMembership | null {
  if (memberships === undefined || memberships.length === 0) {
    return null
  }

  // Copied before sorting: Apollo hands out its cached array, and sorting in
  // place would mutate the cache -- both a write to a frozen object in
  // development and a source of results that differ by read order.
  //
  // `?? null` because `noUncheckedIndexedAccess` cannot see that the length
  // check above makes index 0 present. The fallback is unreachable.
  return (
    [...memberships].sort((left, right) =>
      left.createdAt.localeCompare(right.createdAt),
    )[0] ?? null
  )
}

/** Where a step wants to send the browser instead of rendering. */
export type StepResolution =
  | { redirectTo: OnboardingStep }
  | { redirectTo: 'product' }

/**
 * Where a request for `step` should actually go, or null to render it.
 *
 * `step` is null for `/onboarding` itself, which is never a page.
 *
 * ## The rule, and why it is per-step rather than "the furthest step reached"
 *
 * Each step is gated on the state it actually needs, and only on that:
 *
 *   - `workspace` needs there to be *no* workspace. Once there is one, the
 *     step is finished and offering it again is offering to do finished work
 *     a second time.
 *   - `team`, `invite` and `integrations` each need a workspace, and nothing
 *     more. In particular none of them is gated on `hasTeam` -- the team
 *     step is where a team comes from, and the two optional steps are about
 *     people and services rather than about teams.
 *   - `/onboarding` itself is a router, not a page: to whatever is
 *     unfinished, or into the product when nothing is.
 *
 * That last clause is the requirement that setup is never shown twice. An
 * existing user who lands on `/onboarding` -- from a stale bookmark, from a
 * link in an old email, from typing it -- goes straight to their workspace.
 *
 * ## Why the team step is not gated on `hasTeam`
 *
 * It is the difference between a flow that works and one that races. Creating
 * a team refetches `teams`, which makes `hasTeam` true, which re-renders this
 * guard while the team step is still mounted and about to advance to
 * `invite`. If completing the team step made the team step unreachable, that
 * re-render would fire first and redirect into the product, and nobody would
 * ever see the invite or integrations steps. Gating it on `hasWorkspace`
 * instead means the guard simply does not move, and the step advances itself.
 *
 * The cost is that an existing user who types `/onboarding/team` gets a
 * create-team form. That is a real action they are allowed to take, and they
 * reach it only by typing the URL -- every route into onboarding goes through
 * `/onboarding`, which sends them to the product.
 */
export function resolveStep(
  step: OnboardingStep | null,
  facts: OnboardingFacts,
): StepResolution | null {
  const { hasWorkspace, hasTeam } = facts

  if (step === null) {
    if (!hasWorkspace) {
      return { redirectTo: 'workspace' }
    }

    if (!hasTeam) {
      return { redirectTo: 'team' }
    }

    return { redirectTo: 'product' }
  }

  if (!hasWorkspace) {
    // Nothing but the workspace step can do anything without one -- the
    // other three all send `workspaceSlug` to the server.
    return step === 'workspace' ? null : { redirectTo: 'workspace' }
  }

  if (step === 'workspace') {
    return { redirectTo: hasTeam ? 'invite' : 'team' }
  }

  return null
}

/* --------------------------------------------------------------- routing */

/**
 * The URLs this feature owns.
 *
 * Kept here rather than added to `src/app/routes/paths.ts` on purpose. That
 * file is the one place that knows what a *product* URL looks like, and its
 * whole design is a `scopePrefix` seam so every product path can gain a
 * `/:workspaceSlug` segment at once. Onboarding sits outside that scope by
 * definition -- it is where the workspace comes *from* -- so filing these
 * beside the product paths would mean either excluding them from the seam by
 * hand or dragging them through a scope they can never have. It also keeps
 * this branch from conflicting with the other agents editing that file.
 *
 * The rule that matters is the same one: no component below writes a route
 * as a string literal. If the Lead wants these in `paths.ts`, moving them is
 * one import change.
 */
export const ONBOARDING_ROOT = '/onboarding'

/** Where the copied invite link points. See `pages/AcceptInvitePage`. */
export const INVITE_ROOT = '/invite'

/** The path segment carrying an invitation token, as `useParams` keys it. */
export const INVITE_TOKEN_PARAM = 'token'

export const onboardingPaths = {
  root: () => ONBOARDING_ROOT,
  step: (step: OnboardingStep) => `${ONBOARDING_ROOT}/${step}`,

  // Encoded because the token becomes a path segment. Tokens are
  // `secrets.token_urlsafe(32)` -- 43 base64url characters, so encoding is a
  // no-op today -- but the builder is the right place for the rule, and it
  // will not be revisited if the mint ever changes.
  acceptInvite: (token: string) => `${INVITE_ROOT}/${encodeURIComponent(token)}`,
} as const

/** The step a pathname is asking for, or null for `/onboarding` itself. */
export function stepFromPathname(pathname: string): OnboardingStep | null {
  const segment = pathname.split('/').filter(Boolean).at(-1)

  return ONBOARDING_STEPS.find((step) => step === segment) ?? null
}

/**
 * Where "enter Vector" goes: the issue list of the workspace just set up.
 *
 * Built through `createAppPaths` and not by hand, for the reason
 * `app/routes/paths.ts` gives -- it is the one place that turns a slug into a
 * product URL, and a second spelling is how the two drift.
 *
 * `useAppPaths()` is the hook every other screen uses and is *wrong* here:
 * it reads `:workspaceSlug` from the route params, and onboarding's routes
 * have no such param, so it would build `/issues` -- which now matches
 * `/:workspaceSlug` with the slug `issues` and lands on a workspace nobody
 * has. Onboarding knows its slug from the membership instead, which is the
 * only place it could come from before there is a workspace URL to read it
 * out of.
 */
export function workspaceHome(slug: string): string {
  return createAppPaths(`/${slug}`).issues()
}
