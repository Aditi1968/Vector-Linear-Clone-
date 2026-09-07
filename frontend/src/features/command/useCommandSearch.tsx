import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@apollo/client/react'

import { IssueIcon, ProjectIcon } from '../../components'
import { CommandSearchDocument } from '../../generated/operations'
import { useWorkspace } from '../../app/workspace'
import type { PaletteItem } from './commands'

/**
 * How long the user has to stop typing before the workspace is asked.
 *
 * Short enough that a result feels like it arrived because you typed, long
 * enough that "cycle" is one full-text search rather than five. Every
 * keystroke going straight to the network would be correct and would still be
 * wrong: `websearch_to_tsquery` over a GIN index is cheap, but five of them
 * for one word is five round trips whose first four answers are discarded
 * before they are painted.
 */
const DEBOUNCE_MS = 200

export interface CommandSearchResult {
  issues: readonly PaletteItem[]
  projects: readonly PaletteItem[]
  /** Includes the pause before the request. Typing must not look like nothing. */
  loading: boolean
  /** The search failed. Deliberately a flag: nothing here shows a raw message. */
  failed: boolean
}

/** Nothing, with a stable identity, so an idle palette does not rebuild groups. */
const NONE: readonly PaletteItem[] = []

/**
 * The workspace's issues and projects matching what has been typed, as
 * palette items.
 *
 * ## The workspace comes from the shell, not the URL
 *
 * `useWorkspace().slug` has already been matched against the viewer's own
 * memberships, so the palette cannot ask about a workspace the shell has
 * declined to render. It is not the authorization boundary -- the server
 * authorizes `search` against membership whatever the client sends -- it just
 * means there is one answer to "which tenant is this" on screen at a time.
 *
 * ## Debounced here rather than in the component
 *
 * The palette holds the query because the input does; this holds the *asked*
 * query, which is a different value that lags it. Keeping them in one
 * component would mean the field's value and the request's variable were two
 * `useState`s in the same file, which is exactly how one ends up being reset
 * without the other.
 *
 * An empty query skips the request entirely rather than sending one the
 * server will short-circuit: `SearchService` returns empty results for a
 * blank query without touching the database, so the round trip would be
 * spent establishing something both ends already know.
 */
export function useCommandSearch(query: string): CommandSearchResult {
  const { slug, paths } = useWorkspace()
  const navigate = useNavigate()

  const trimmed = query.trim()
  const [asked, setAsked] = useState(trimmed)

  useEffect(() => {
    const timer = setTimeout(() => {
      setAsked(trimmed)
    }, DEBOUNCE_MS)

    /* Cleared on every change, which is the whole debounce: the timer only
     * ever fires for the last keystroke in a burst. */
    return () => {
      clearTimeout(timer)
    }
  }, [trimmed])

  const { data, loading, error } = useQuery(CommandSearchDocument, {
    variables: { workspaceSlug: slug, query: asked },
    skip: asked === '',
  })

  const results = asked === '' ? undefined : data?.search

  const issues = useMemo<readonly PaletteItem[]>(() => {
    if (results === undefined) {
      return NONE
    }

    return results.issues.map((issue) => ({
      id: `issue-${issue.id}`,
      label: issue.title,
      hint: issue.identifier,
      icon: <IssueIcon />,
      run: () => {
        void navigate(paths.issue(issue.id))
      },
    }))
  }, [navigate, paths, results])

  const projects = useMemo<readonly PaletteItem[]>(() => {
    if (results === undefined) {
      return NONE
    }

    return results.projects.map((project) => ({
      id: `project-${project.id}`,
      label: project.name,
      icon: <ProjectIcon />,
      run: () => {
        void navigate(paths.project(project.id))
      },
    }))
  }, [navigate, paths, results])

  return {
    issues,
    projects,
    /* The pause counts as loading. Otherwise the 200ms after the last
     * keystroke is a window where the palette shows stale results and claims
     * to be idle, which reads as "it found nothing" rather than "it is
     * still looking". */
    loading: trimmed !== '' && (trimmed !== asked || loading),
    failed: error !== undefined,
  }
}
