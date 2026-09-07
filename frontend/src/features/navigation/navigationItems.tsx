import type { ReactNode } from 'react'

import {
  BoardIcon,
  InboxIcon,
  IssueIcon,
  IssuesIcon,
  ProjectIcon,
  SettingsIcon,
  TeamIcon,
} from '../../components'
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
   * called at module scope where these arrays are defined; taking the path
   * set as an argument lets the nav stay declarative data while still
   * resolving its URLs through the one place that knows what a Vector URL
   * looks like. Writing `to: '/issues'` here would work against a
   * single-workspace development database and break in production.
   */
  to: (paths: AppPaths) => string
  /**
   * Match the destination exactly rather than as a prefix.
   *
   * Prefix matching is the default and is usually right: it keeps All Issues
   * marked as the current page while the user reads
   * `/:slug/issues/:issueId`, because a detail view is somewhere *inside*
   * that section, and a sidebar that de-highlights when you open a row tells
   * the user they have left the section they are still in.
   */
  end?: boolean
}

/**
 * The workspace's primary surfaces.
 *
 * Every one is a real backend surface -- issues, notifications and projects
 * all have root fields -- and every one has a route. Some of the screens
 * behind them are not built yet and say so; `app/routes/Placeholder.tsx`
 * records why that beats a dead entry.
 *
 * Cycles is deliberately absent. `cycles(workspaceSlug:, teamId:)` is
 * team-scoped, so a workspace-wide Cycles entry would have to pick one team
 * to link to, and a link that silently chooses one team out of several is a
 * lie about what the user is about to look at. Cycles appears under each team
 * in ./TeamsSection.tsx, which is where the concept actually lives.
 */
export const primaryNavigationItems: readonly NavigationItem[] = [
  {
    id: 'my-issues',
    label: 'My Issues',
    icon: <IssueIcon />,
    to: (paths) => paths.myIssues(),
  },
  {
    id: 'inbox',
    label: 'Inbox',
    icon: <InboxIcon />,
    to: (paths) => paths.inbox(),
  },
  {
    id: 'issues',
    label: 'All Issues',
    icon: <IssuesIcon />,
    to: (paths) => paths.issues(),
  },
  {
    /*
     * The board takes no team in its path -- it carries one in the query
     * string, and this entry deliberately links without it. A sidebar link
     * that named a team would have to pick one on the user's behalf; the
     * screen picks the workspace's first team, says which one it is, and
     * offers the picker.
     */
    id: 'board',
    label: 'Board',
    icon: <BoardIcon />,
    to: (paths) => paths.board(),
  },
  {
    id: 'projects',
    label: 'Projects',
    icon: <ProjectIcon />,
    to: (paths) => paths.projects(),
  },
]

/** The workspace itself, rather than the work inside it. */
export const workspaceNavigationItems: readonly NavigationItem[] = [
  {
    id: 'members',
    label: 'Members',
    icon: <TeamIcon />,
    to: (paths) => paths.members(),
  },
  {
    id: 'settings',
    label: 'Settings',
    icon: <SettingsIcon />,
    to: (paths) => paths.settings(),
  },
]
