import { createContext, useContext } from 'react'

import type { AppPaths } from '../routes/paths'
import type { WorkspaceShellQuery } from '../../generated/operations'

/**
 * The workspace the viewer is currently inside.
 *
 * Published by `AppLayout` once -- and only once -- the slug in the URL has
 * been matched against the caller's own memberships. Everything below the
 * shell can therefore treat `slug` as a fact rather than as a route parameter
 * it has to re-check, which is the whole reason the context exists: a screen
 * that reached for `useParams().workspaceSlug` would be reading the address
 * bar, and the address bar is user input.
 *
 * That is also why every page's `workspaceSlug` query variable should come
 * from here. It is not an authorization decision -- the server authorizes
 * every scoped field against membership regardless of what the client sends
 * -- it just means a page cannot accidentally query with a slug the shell has
 * already rejected.
 */

/** One membership, exactly as the shell query selects it. */
export type WorkspaceMembership = WorkspaceShellQuery['myWorkspaces'][number]

/** The signed-in user, exactly as the shell query selects it. */
export type Viewer = NonNullable<WorkspaceShellQuery['me']>

export interface WorkspaceContextValue {
  /** The workspace's URL segment, confirmed to be one the viewer belongs to. */
  slug: string
  /** Its id and display name. */
  workspace: WorkspaceMembership['workspace']
  /** The viewer's role here. `MEMBER`, `ADMIN` or `OWNER`. */
  role: WorkspaceMembership['role']
  /** Every workspace the viewer belongs to, for the switcher. */
  memberships: readonly WorkspaceMembership[]
  /** The signed-in user, or null if the session ended under us. */
  viewer: Viewer | null
  /** This workspace's path builders. See ../routes/paths.ts. */
  paths: AppPaths
}

const WorkspaceContext = createContext<WorkspaceContextValue | null>(null)

export const WorkspaceProvider = WorkspaceContext.Provider

/**
 * The current workspace.
 *
 * Throws outside the shell rather than returning null. There is no sensible
 * fallback -- a screen without a workspace cannot build a URL or a query
 * variable -- and a hook that returned `undefined` here would turn a mounting
 * mistake into a blank page somewhere else.
 */
export function useWorkspace(): WorkspaceContextValue {
  const value = useContext(WorkspaceContext)

  if (value === null) {
    throw new Error('useWorkspace() was called outside the workspace shell')
  }

  return value
}
