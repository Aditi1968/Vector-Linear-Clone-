import { useMemo } from 'react'
import { Navigate, Outlet, useParams } from 'react-router-dom'
import { useQuery } from '@apollo/client/react'

import { WorkspaceShellDocument } from '../../generated/operations'
import { ONBOARDING_PATH, WORKSPACE_SLUG_PARAM, createAppPaths } from '../routes/paths'
import { WorkspaceProvider } from '../workspace/context'
import type { WorkspaceContextValue } from '../workspace/context'
import { CreateIssueActionProvider } from './CreateIssueActionProvider'
import { ShellError, ShellSkeleton, WorkspaceNotFound } from './ShellStates'
import { Sidebar } from './Sidebar'
import { useSidebarCollapsed } from './preferences'
import styles from './AppLayout.module.css'

/**
 * The id the skip link targets and `<main>` carries.
 *
 * Exported so that a test, or anything that later needs to move focus to the
 * content region, refers to the same string this file does rather than
 * repeating a fragment that only breaks when someone renames it here.
 */
export const MAIN_CONTENT_ID = 'main-content'

/** The rail's `<nav>`, so the collapse toggle can name it in `aria-controls`. */
export const SIDEBAR_NAV_ID = 'sidebar-navigation'

/**
 * Vector's workspace shell: persistent rail on the left, page on the right.
 *
 * Mounted as the element of the `/:workspaceSlug` route, so it renders once
 * and survives every navigation within a workspace. Chrome that unmounts and
 * remounts loses focus and scroll position on every route change, which is
 * the difference between an application and a website.
 *
 * ## The slug is a route parameter, not a claim
 *
 * `/:workspaceSlug` is a string the user can type. This component turns it
 * into a workspace by looking for it in `myWorkspaces` -- the caller's own
 * memberships -- and never by asking the server about the slug itself. Three
 * consequences, all deliberate:
 *
 *   - a slug the viewer is not a member of is indistinguishable from one that
 *     does not exist, because the client never learns either way;
 *   - the switcher's data and the membership check are one response, so the
 *     check costs no extra request;
 *   - nothing below the shell re-checks anything: `useWorkspace()` hands out
 *     a slug that has already been matched.
 *
 * None of that is the security boundary. The server authorizes every scoped
 * field against membership on its own and answers NOT_FOUND identically for a
 * workspace that does not exist and one the caller may not see. This is the
 * client declining to render a shell it has no reason to believe in.
 *
 * ## Four states, in order
 *
 * Loading, failed, no workspace at all, and a slug that is not one of yours.
 * The third is a redirect rather than a screen -- a signed-in user with no
 * workspace has exactly one thing to do next, and `features/onboarding` owns
 * the screen that does it.
 */
export function AppLayout() {
  const slug = useParams()[WORKSPACE_SLUG_PARAM]
  const { data, loading, error, refetch } = useQuery(WorkspaceShellDocument)
  const [collapsed, toggleCollapsed] = useSidebarCollapsed()

  const memberships = data?.myWorkspaces

  const membership =
    memberships?.find((entry) => entry.workspace.slug === slug) ?? null

  /*
    Memoised on the membership rather than rebuilt every render: `paths` is a
    fresh object of fresh closures each time it is built, and this value is
    read by every link in the rail.
  */
  const viewer = data?.me ?? null

  const workspace = useMemo<WorkspaceContextValue | null>(() => {
    if (membership === null || memberships === undefined) {
      return null
    }

    return {
      slug: membership.workspace.slug,
      workspace: membership.workspace,
      role: membership.role,
      memberships,
      viewer,
      paths: createAppPaths(membership.workspace.slug),
    }
  }, [membership, memberships, viewer])

  if (loading && data === undefined) {
    return <ShellSkeleton />
  }

  if (error !== undefined || memberships === undefined) {
    return (
      <ShellError
        onRetry={() => {
          void refetch()
        }}
      />
    )
  }

  // A signed-in user with nowhere to be. `replace` so the address they could
  // not use does not sit in history waiting for the back button.
  if (memberships.length === 0) {
    return <Navigate to={ONBOARDING_PATH} replace />
  }

  if (workspace === null) {
    return <WorkspaceNotFound slug={slug ?? ''} memberships={memberships} />
  }

  /*
    Three things are established below and nowhere else:

      - the `main` landmark, and the skip link that reaches it;
      - the create-issue slot (see ./createIssueAction.ts), wrapped around
        both the rail that reads it and the `<Outlet />` that fills it;
      - the two-column geometry, in AppLayout.module.css.

    The skip link is the first focusable element in the document, which is the
    only position it works from. `<main>` carries `tabIndex={-1}`: without it,
    following the skip link moves the viewport but leaves focus on the link,
    so the next Tab lands back at the top of the rail -- the exact loop the
    link exists to break.
  */
  return (
    <WorkspaceProvider value={workspace}>
      <CreateIssueActionProvider>
        <div className={styles.shell}>
          <a className={styles.skipLink} href={`#${MAIN_CONTENT_ID}`}>
            Skip to main content
          </a>

          <Sidebar collapsed={collapsed} onToggleCollapsed={toggleCollapsed} />

          <main id={MAIN_CONTENT_ID} className={styles.main} tabIndex={-1}>
            <Outlet />
          </main>
        </div>
      </CreateIssueActionProvider>
    </WorkspaceProvider>
  )
}
