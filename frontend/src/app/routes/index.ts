/**
 * Routing's public surface.
 *
 * Every URL in the product is built from here. A component that writes a
 * route as a string literal has hardcoded the workspace layout of the
 * application; see ./paths.ts.
 *
 * The route *table* is deliberately not re-exported. It imports every
 * feature's route array, and several of those features import this module for
 * their path helpers -- so a barrel that pulled ./routes in would make every
 * one of them a circular import, and the one that lost the race would spread
 * an undefined array at module scope. `App.tsx` and the test harness import
 * `./routes/routes` directly, and they are the only two things that need it.
 */

export { useAppPaths, useWorkspaceSlug } from './useAppPaths'

export {
  CYCLE_ID_PARAM,
  ISSUE_ID_PARAM,
  ONBOARDING_PATH,
  PROJECT_ID_PARAM,
  ROUTE_SEGMENTS,
  TEAM_KEY_PARAM,
  WORKSPACE_SLUG_PARAM,
  createAppPaths,
  paths,
} from './paths'
export type { AppPaths, WorkspaceScopedPaths } from './paths'

/* For the auth feature, whose signed-in landing has the same job: resolve a
 * workspace when the URL names none. */
export { WorkspaceEntry } from './WorkspaceEntry'

/* For an agent mounting a screen that is not built yet. */
export { Placeholder } from './Placeholder'
export type { PlaceholderProps } from './Placeholder'
