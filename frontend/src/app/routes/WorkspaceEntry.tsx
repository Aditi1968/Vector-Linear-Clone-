import { Navigate } from 'react-router-dom'
import { useQuery } from '@apollo/client/react'

import { WorkspaceEntryDocument } from '../../generated/operations'
import { ONBOARDING_PATH, paths } from './paths'

/**
 * What `/` resolves to now that every screen lives under a workspace.
 *
 * The API requires a `workspaceSlug` on every field it exposes, so there is
 * no workspace-less screen left to render and `/` cannot be a page. It is a
 * redirect, and the only question is where to.
 *
 * The first membership, deliberately, and deliberately not remembered. A
 * "last workspace" in localStorage would be a second source of truth for
 * something the URL already states, and the URL is the one that survives a
 * link being shared. A real switcher -- with an order the user chose, and a
 * memory of where they were -- belongs to the workspace shell, which is
 * somebody else's work; this is the smallest thing that gets a signed-in
 * user to their issues.
 *
 * Four states, and three of them are not errors:
 *
 *   * loading: nothing, rather than a spinner. This resolves in one round
 *     trip and a flash of chrome that is replaced immediately reads as a
 *     glitch.
 *   * a membership: redirect, replacing history so `/` does not sit in the
 *     back stack and trap the button on a URL that only ever redirects.
 *   * no memberships: a real state -- an account created but not invited
 *     anywhere -- so it goes to onboarding rather than looking broken.
 *   * an error: including UNAUTHENTICATED, which is what an unauthenticated
 *     visitor gets from `myWorkspaces`. There is no login screen to send
 *     them to yet, so this reports that it could not tell and stops.
 *
 * Standalone markup rather than the shell's `PageHeader`/`PageContent`: this
 * renders OUTSIDE `AppLayout` (see ./routes), because the shell's sidebar
 * links are built from a workspace slug this component exists to find.
 */
export function WorkspaceEntry() {
  const { data, loading, error } = useQuery(WorkspaceEntryDocument)

  if (loading) {
    return null
  }

  if (error !== undefined) {
    return (
      <main>
        <h1>Could not open a workspace</h1>
        <p>Vector could not tell which workspaces you belong to.</p>
      </main>
    )
  }

  const first = data?.myWorkspaces[0]

  // A signed-in account in no workspace has exactly one thing to do next,
  // and `features/onboarding` owns the screen that does it.
  if (first === undefined) {
    return <Navigate to={ONBOARDING_PATH} replace />
  }

  // Through the path builder rather than a template literal, for the reason
  // ./paths.ts gives: this is the one place that turns a slug into a URL,
  // and a second spelling of the same URL is how the two drift.
  return <Navigate to={paths.issues(first.workspace.slug)} replace />
}
