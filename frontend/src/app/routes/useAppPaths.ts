import { appPaths } from './paths'
import type { AppPaths } from './paths'

/**
 * The path set for the currently active scope.
 *
 * Components build every internal URL through this hook -- `useAppPaths()
 * .issue(id)` -- and never through a string literal or through `appPaths`
 * directly.
 *
 * Today it returns the unscoped paths, and the indirection looks redundant.
 * It is not: it is the whole mechanism by which backend Phase 1b-5 avoids
 * touching feature components. When routes gain a `/:workspaceSlug` segment,
 * this function becomes
 *
 *     export function useAppPaths(): AppPaths {
 *       const { workspaceSlug } = useParams()
 *       return useMemo(
 *         () => createAppPaths(workspaceSlug ? `/${workspaceSlug}` : ''),
 *         [workspaceSlug],
 *       )
 *     }
 *
 * and every call site keeps working unchanged, because a call site only ever
 * asked for "the path to this issue" and never for "the path to this issue
 * in this workspace". A component that had reached for `appPaths` directly,
 * or written a template literal, would have had to change.
 *
 * Which is also why this is a hook rather than a plain function today: the
 * scoped version must read route params, and only a hook can. Making it a
 * hook now means the signature does not change later.
 */
export function useAppPaths(): AppPaths {
  return appPaths
}
