import { useMemo } from 'react'
import { useParams } from 'react-router-dom'

import { WORKSPACE_SLUG_PARAM, createAppPaths } from './paths'
import type { AppPaths } from './paths'

/**
 * The workspace the URL is currently addressing.
 *
 * The one place a slug enters the application. Every field the API exposes
 * names a workspace, so this is what turns "which URL is open" into "which
 * tenant is being asked about" -- and the URL is deliberately the only
 * source. A slug held in state or in storage would be a second answer to that
 * question, and the one that disagrees with the address bar is the one a
 * shared link produces.
 *
 * `''` when there is no param, which happens only outside the workspace
 * routes. It is not a slug any workspace holds, so a request made with it is
 * refused as a workspace the viewer cannot see -- the same answer any other
 * wrong slug gets. That is the right failure: a caller with no workspace has
 * nothing to ask about, and inventing a default here is exactly what the
 * server's bootstrap tenant used to do.
 *
 * This reads the raw parameter on purpose, rather than the slug `AppLayout`
 * confirmed against `myWorkspaces`. The confirmed one is in
 * `useWorkspace().slug` and is the right thing to render *identity* from; for
 * building a URL or a query variable the address bar is both sufficient and
 * safer, because it is the value the server will be asked to authorize and
 * there is no way for a screen to send one tenant's slug while displaying
 * another's.
 */
export function useWorkspaceSlug(): string {
  return useParams()[WORKSPACE_SLUG_PARAM] ?? ''
}

/**
 * The path set for the currently active workspace.
 *
 * Components build every internal URL through this hook -- `useAppPaths()
 * .issue(id)` -- and never through a string literal. That rule is what made
 * adding `/:workspaceSlug` a change to this function rather than to every
 * screen that renders a link: a call site only ever asked for "the path to
 * this issue", never for "the path to this issue in this workspace".
 *
 * Use `paths` from ./paths.ts directly when the workspace is not the current
 * one -- the switcher links into *other* workspaces, and no hook could know
 * which.
 *
 * Memoised on the slug so the returned object is stable across renders, which
 * is what keeps a memoised child taking a path builder as a prop from
 * re-rendering on every parent render.
 */
export function useAppPaths(): AppPaths {
  const workspaceSlug = useWorkspaceSlug()

  return useMemo(() => createAppPaths(workspaceSlug), [workspaceSlug])
}
