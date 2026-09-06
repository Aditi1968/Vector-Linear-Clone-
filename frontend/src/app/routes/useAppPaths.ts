import { useMemo } from 'react'
import { useParams } from 'react-router-dom'

import { createAppPaths, WORKSPACE_SLUG_PARAM } from './paths'
import type { AppPaths } from './paths'

/**
 * The workspace the URL is currently addressing.
 *
 * The one place a slug enters the application. Every field the API exposes
 * now names a workspace, so this is what turns "which URL is open" into
 * "which tenant is being asked about" -- and the URL is deliberately the
 * only source. A slug held in state or in storage would be a second answer
 * to that question, and the one that disagrees with the address bar is the
 * one a shared link produces.
 *
 * `''` when there is no param, which happens only outside the workspace
 * routes. It is not a slug any workspace holds, so a request made with it is
 * refused as a workspace the viewer cannot see -- the same answer any other
 * wrong slug gets. That is the right failure: a caller with no workspace has
 * nothing to ask about, and inventing a default here is exactly what
 * `BOOTSTRAP_WORKSPACE_SLUG` used to do on the server.
 */
export function useWorkspaceSlug(): string {
  const params = useParams()

  return params[WORKSPACE_SLUG_PARAM] ?? ''
}

/**
 * The path set for the currently active scope.
 *
 * Components build every internal URL through this hook -- `useAppPaths()
 * .issue(id)` -- and never through a string literal or through `appPaths`
 * directly. That rule is what made adding `/:workspaceSlug` a change to this
 * function rather than to every screen that renders a link: a call site only
 * ever asked for "the path to this issue", never for "the path to this issue
 * in this workspace".
 *
 * A hook and not a plain function because it reads a route param, and only a
 * hook can. Memoised on the slug so the returned object is stable across
 * renders, which is what keeps a memoised child taking a path builder as a
 * prop from re-rendering on every parent render.
 */
export function useAppPaths(): AppPaths {
  const workspaceSlug = useWorkspaceSlug()

  return useMemo(
    () => createAppPaths(workspaceSlug === '' ? '' : `/${workspaceSlug}`),
    [workspaceSlug],
  )
}
