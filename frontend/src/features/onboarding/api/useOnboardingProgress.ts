import { useQuery } from '@apollo/client/react'

import { selectOnboardingWorkspace } from '../lib/progress'
import type { OnboardingMembership } from '../lib/progress'
import { describeError } from '../lib/errors'
import { OnboardingTeamsDocument, OnboardingWorkspacesDocument } from './documents'

export interface OnboardingProgress {
  /** Progress is not known yet. Nothing may be redirected on an unknown. */
  loading: boolean
  /** A transport or top-level failure. Field errors never arrive here. */
  error: string | null
  /**
   * The workspace setup is about, or null when there is not one yet.
   *
   * Null is `hasWorkspace: false` -- the first of the two facts the state
   * machine is made of. See ../lib/progress.ts.
   */
  membership: OnboardingMembership | null
  /** The second fact: `teams(workspaceSlug:)` came back non-empty. */
  hasTeam: boolean
  /** Re-derive from the server. The retry control on the error state. */
  refetch: () => void
}

/**
 * Onboarding progress, derived from server state on every render.
 *
 * Two queries and no stored cursor. `myWorkspaces` empty means the workspace
 * step is unfinished; `teams(workspaceSlug:)` empty means the team step is
 * unfinished. That is the whole machine (../lib/progress.ts holds the rules
 * themselves, as pure functions).
 *
 * Being derived is what makes a refresh mid-setup resume rather than
 * restart, and it is why there is no `onboarding_step` column, no
 * localStorage key and no wizard cursor anywhere in this feature. A stored
 * cursor is a second source of truth, and it disagrees with the first at
 * exactly the worst moment: when a step has just been completed and the
 * write recording that fails.
 */
export function useOnboardingProgress(): OnboardingProgress {
  const workspaces = useQuery(OnboardingWorkspacesDocument, {
    // The answer decides whether a redirect happens, so a stale one is a
    // redirect to the wrong step. `cache-and-network` renders the cached
    // answer immediately -- which is what makes moving between steps
    // instant -- while revalidating, so a workspace created in another tab
    // is picked up rather than believed absent forever.
    fetchPolicy: 'cache-and-network',
  })

  const membership = selectOnboardingWorkspace(workspaces.data?.myWorkspaces)

  const teams = useQuery(OnboardingTeamsDocument, {
    // `skip` makes the variables unreachable, but they are still type-checked
    // as `String!`, so a placeholder is required. It is never sent.
    variables: { workspaceSlug: membership?.workspace.slug ?? '' },
    skip: membership === null,
    fetchPolicy: 'cache-and-network',
  })

  /*
   * "Do we know yet?", not "is a request in flight?".
   *
   * `loading` alone is the wrong question in two places, and both produce a
   * wrong *redirect* rather than a wrong spinner -- which is the failure
   * that actually hurts:
   *
   *   - `cache-and-network` reports `loading: true` on a revalidation that
   *     already has cached data. Treating that as unknown would blank the
   *     step every time it revalidated.
   *   - between the workspace arriving and the teams query starting, the
   *     teams query is neither loading nor answered. Reading `data` as
   *     "empty" there would bounce a user with a team onto the team step
   *     for a frame.
   *
   * So: known once data has arrived, or once the query has failed.
   */
  const workspacesKnown =
    workspaces.data !== undefined || workspaces.error !== undefined

  const teamsKnown =
    membership === null || teams.data !== undefined || teams.error !== undefined

  const failure = workspaces.error ?? teams.error

  return {
    loading: !workspacesKnown || !teamsKnown,
    error: failure === undefined ? null : describeError(failure),
    membership,
    hasTeam: (teams.data?.teams.length ?? 0) > 0,
    refetch: () => {
      // Both, because either can be the one that failed and the retry
      // control does not know which. A refetch of a skipped query is a
      // no-op.
      void workspaces.refetch()
      void teams.refetch()
    },
  }
}
