import { useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import type { ReactNode } from 'react'

import {
  CycleIcon,
  InboxIcon,
  IssueIcon,
  IssuesIcon,
  ProjectIcon,
  SearchIcon,
  SettingsIcon,
  TeamIcon,
} from '../../components'
import { useWorkspace } from '../../app/workspace'

/**
 * One thing the palette can do.
 *
 * `run` and not `href`, because an option in a listbox cannot also be a link:
 * `role="option"` replaces whatever role the element had, so a `role="option"`
 * anchor is an option that no longer announces as a link and no longer opens
 * in a new tab from the context menu. The palette is a command surface rather
 * than a set of links that happen to be listed, and the navigation goes
 * through the router either way.
 */
export interface PaletteItem {
  /** Unique across every group. Becomes half of the option's DOM id. */
  id: string
  /** The visible label, and what the query is matched against. */
  label: string
  /** Secondary text on the right -- an issue's identifier, a group's kind. */
  hint?: string
  /** Decorative glyph. The label carries the meaning. */
  icon: ReactNode
  /** Leave the palette open after running. For commands that change the view. */
  keepOpen?: boolean
  run: () => void
}

export interface PaletteGroup {
  id: string
  /** The group heading, and the group's accessible name. */
  label: string
  items: readonly PaletteItem[]
}

/**
 * Substring matching, case-insensitively, on the label.
 *
 * Deliberately not fuzzy. A fuzzy matcher is the thing everyone reaches for
 * here and it is a scoring function plus a tie-break plus a highlight
 * renderer, all to rank a list that is nine items long. Substring matching is
 * one line, is never surprising, and the *interesting* half of the results --
 * issues and projects -- is ranked by PostgreSQL, which has the whole
 * workspace to rank and a `ts_rank` to do it with.
 */
export function filterItems(
  items: readonly PaletteItem[],
  query: string,
): readonly PaletteItem[] {
  const needle = query.trim().toLowerCase()

  if (needle === '') {
    return items
  }

  return items.filter((item) => item.label.toLowerCase().includes(needle))
}

/**
 * Every place in this workspace the palette can send you.
 *
 * Written out rather than derived from the rail's `navigationItems`, because
 * the two lists are not the same list and should not be forced to be. The
 * rail deliberately omits Cycles -- a workspace-level cycles link would have
 * to pick a team, and the rail shows cycles under each team instead -- while
 * the palette's Cycles command lands on the screen that has a team picker on
 * it, so here it is honest. Search is likewise a rail affordance rather than
 * a rail nav item.
 *
 * Every destination comes from the workspace's own path builders. No route is
 * a string literal here, which is what made adding the workspace segment a
 * change to `app/routes/paths.ts` rather than a sweep.
 */
export function useNavigationCommands(): readonly PaletteItem[] {
  const { paths } = useWorkspace()
  const navigate = useNavigate()

  return useMemo(() => {
    /* `void`: `navigate` returns a promise under React Router 7's data
     * router, and nothing here waits on it. */
    const go = (to: string) => () => {
      void navigate(to)
    }

    return [
      { id: 'go-my-issues', label: 'My Issues', icon: <IssueIcon />, run: go(paths.myIssues()) },
      { id: 'go-inbox', label: 'Inbox', icon: <InboxIcon />, run: go(paths.inbox()) },
      { id: 'go-issues', label: 'All Issues', icon: <IssuesIcon />, run: go(paths.issues()) },
      { id: 'go-projects', label: 'Projects', icon: <ProjectIcon />, run: go(paths.projects()) },
      { id: 'go-cycles', label: 'Cycles', icon: <CycleIcon />, run: go(paths.cycles()) },
      { id: 'go-search', label: 'Search', icon: <SearchIcon />, run: go(paths.search()) },
      { id: 'go-members', label: 'Members', icon: <TeamIcon />, run: go(paths.members()) },
      { id: 'go-settings', label: 'Settings', icon: <SettingsIcon />, run: go(paths.settings()) },
    ]
  }, [navigate, paths])
}
