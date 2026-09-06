import type { ReactNode } from 'react'

import { CycleIcon, IssuesIcon, ProjectIcon } from '../../components'
import type { AppPaths } from '../../app/routes'

export interface NavigationItem {
  /** Stable React key. Not a path -- paths change, this must not. */
  id: string
  /** The visible label, and the link's accessible name. */
  label: string
  /** Decorative glyph; the label carries the meaning. */
  icon: ReactNode
  /**
   * The destination, derived from the app's path helpers rather than written
   * out.
   *
   * A function of `AppPaths` rather than a plain string, and that is the
   * whole design of this module. `useAppPaths()` is a hook, so it cannot be
   * called at module scope where this array is defined; taking the path set
   * as an argument lets the nav stay declarative data while still resolving
   * its URLs through the one place that knows what a Vector URL looks like.
   * When backend Phase 1b-5 puts a `/:workspaceSlug` in front of every route,
   * this file does not change at all -- `paths.issues()` starts returning the
   * scoped URL and the link follows.
   *
   * Writing `to: '/issues'` here would work today and silently break then.
   */
  to: (paths: AppPaths) => string
  /**
   * Match the destination exactly rather than as a prefix.
   *
   * Left off for Issues on purpose: prefix matching is what keeps the Issues
   * item marked as the current page while the user is reading
   * `/issues/:issueId`. A detail view is somewhere *inside* Issues, and a
   * sidebar that de-highlights when you open a row tells the user they have
   * left the section they are still in.
   */
  end?: boolean
}

/**
 * The sidebar's navigation.
 *
 * Three items, and the rule that governs the list has not changed: a surface
 * appears here when the backend can serve it, and not before. Inbox, Views,
 * Teams, Members and Settings are still absent rather than
 * present-and-greyed, because a greyed row still makes a claim -- it says the
 * feature exists and is temporarily unavailable, which is a different and
 * false statement about a product where the schema, the resolvers and the
 * tables for those concepts do not exist. Listing them would also quietly
 * commit the backend to a roadmap the frontend has no standing to set.
 *
 * Projects and Cycles joined Issues because `projects`, `project`, `cycles`
 * and `cycle` are now real query fields with real resolvers behind them --
 * which is exactly the condition this list was written to wait for.
 */
export const primaryNavigationItems: readonly NavigationItem[] = [
  {
    id: 'issues',
    label: 'Issues',
    icon: <IssuesIcon />,
    to: (paths) => paths.issues(),
  },
  {
    id: 'projects',
    label: 'Projects',
    icon: <ProjectIcon />,
    to: (paths) => paths.projects(),
  },
  {
    id: 'cycles',
    label: 'Cycles',
    icon: <CycleIcon />,
    to: (paths) => paths.cycles(),
  },
]
