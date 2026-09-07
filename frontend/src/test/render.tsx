import { render, screen, within } from '@testing-library/react'
import type { RenderResult } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { UserEvent } from '@testing-library/user-event'
import { StrictMode } from 'react'
import { RouterProvider, createMemoryRouter } from 'react-router-dom'

import { AppProviders } from '../app/providers/AppProviders'
import { routes } from '../app/routes/routes'
import { createTestClient } from './client'
import { ControlledLink } from './controlledLink'
import { WORKSPACE_SLUG, viewerData, workspaceShellData } from './factories'
import type {
  MeQuery,
  ShellSidebarQuery,
  WorkspaceShellQuery,
} from '../generated/operations'

/**
 * Mount the real application against a controllable network.
 *
 * Everything below the link is production code: the real route table, the
 * real `AppLayout`, the real pages, the real hooks, and -- the part that
 * matters most for the pagination tests -- the real `createCache()`, which is
 * the only place the `issues` field policy exists. A test that swapped in a
 * plain `new InMemoryCache()` would still render rows and would still pass a
 * naive "two pages appear" assertion, while proving nothing about the merge,
 * the dedupe or the `after: null` reset. So the cache is never stubbed here,
 * and there is no second copy of the policy in this directory to drift from
 * the first.
 *
 * A fresh client per render, because a cache is mutable state: sharing one
 * between tests would let a list loaded in one test satisfy a query in the
 * next, which is the kind of leak that makes a suite pass in order and fail
 * in isolation.
 */

export interface RenderAppOptions {
  /**
   * Where the router starts. Defaults to the issue list of `WORKSPACE_SLUG`.
   *
   * Every screen is addressed as `/:workspaceSlug/...`, because every field
   * the API exposes takes a workspace and the frontend reads it from the
   * URL. A path without the segment is not an unscoped screen -- it is a
   * request for a workspace whose slug happens to be `issues`.
   */
  initialPath?: string
  /**
   * Mount inside `<StrictMode>`, as `src/main.tsx` does.
   *
   * Off by default. StrictMode double-invokes effects, which is a stress test
   * worth running deliberately -- it is the condition the cache policy's
   * `after: null` reset branch and the create-action slot's identity-checked
   * cleanup both exist to survive -- but it also makes "how many requests
   * were sent" an ambiguous question, and most tests here ask exactly that.
   */
  strictMode?: boolean
  /**
   * The shell's own two queries, answered as a standing response.
   *
   * `AppLayout` will not render a screen until `WorkspaceShell` has told it
   * the viewer belongs to the workspace in the URL, and the rail asks for
   * `ShellSidebar` straight after. Neither is what most tests are about, so
   * both are answered by default and never appear as pending requests -- see
   * `ControlledLink.answerAlways`.
   *
   * `null` leaves one unanswered, which is how the shell's own tests drive
   * its loading, error and not-found states.
   *
   * `sidebar` defaults to `null` and `shell` does not, which is not an
   * oversight. `ShellSidebar` selects `teams(workspaceSlug:)`, and so does
   * `WorkspaceTeams` in `features/issues/api` -- same root field, same
   * arguments, so answering the sidebar's copy writes a cache entry that the
   * composer's copy then reads instead of going to the network. That is the
   * right behaviour in the product (one request instead of two) and the wrong
   * default for a suite where several tests assert on `WorkspaceTeams` being
   * dispatched. Leaving it unanswered keeps the rail's teams in their loading
   * state, which no test outside the shell's own looks at.
   */
  shell?: WorkspaceShellQuery | null
  sidebar?: ShellSidebarQuery | null
  /**
   * Who is signed in, answered as a standing response.
   *
   * `RequireAuth` resolves the viewer before any authenticated screen mounts,
   * so without this every test below the guard would render the redirect to
   * `/login` instead of the page it is about. Answered by default for the
   * same reason `shell` is; `null` leaves it unanswered, which is how the
   * auth tests drive the signed-out and still-resolving states.
   */
  viewer?: MeQuery | null
}

export interface RenderAppResult extends RenderResult {
  /** The network. Nothing resolves until this is told to. */
  link: ControlledLink
  /** `userEvent`, already set up, so tests do not each configure one. */
  user: UserEvent
  /** The URL the router is currently showing. */
  currentPath: () => string
  /**
   * Its query string, `?` included, or `''` when there is none.
   *
   * Separate from `currentPath` because it answers a different question: a
   * screen that keeps its view state in the query string -- the board's
   * filters, sort and grouping -- is claiming that the URL *is* the state, and
   * the only way to test that claim is to read what a control wrote there.
   */
  currentSearch: () => string
}

export function renderApp({
  initialPath = `/${WORKSPACE_SLUG}/issues`,
  strictMode = false,
  shell = workspaceShellData(),
  sidebar = null,
  viewer = viewerData(),
}: RenderAppOptions = {}): RenderAppResult {
  const link = new ControlledLink()

  if (viewer !== null) {
    link.answerAlways('Me', { data: viewer })
  }

  if (shell !== null) {
    link.answerAlways('WorkspaceShell', { data: shell })
  }

  if (sidebar !== null) {
    link.answerAlways('ShellSidebar', { data: sidebar })
  }

  const client = createTestClient(link)

  const router = createMemoryRouter(routes, { initialEntries: [initialPath] })

  const tree = (
    <AppProviders client={client}>
      <RouterProvider router={router} />
    </AppProviders>
  )

  const view = render(strictMode ? <StrictMode>{tree}</StrictMode> : tree)

  return {
    ...view,
    link,
    user: userEvent.setup(),
    currentPath: () => router.state.location.pathname,
    currentSearch: () => router.state.location.search,
  }
}

/**
 * The page's content region.
 *
 * Queries are scoped through this rather than through `screen` wherever the
 * shell renders something similar -- the sidebar has a list of navigation
 * links, and an unscoped `getAllByRole('link')` would count them as issue
 * rows.
 */
export function main(): HTMLElement {
  return screen.getByRole('main')
}

/** The issue list itself. Throws if the list is not on screen. */
export function issueList(): HTMLElement {
  return within(main()).getByRole('list')
}

/**
 * The rows currently rendered, in order.
 *
 * Whole rows are links (see `IssueRow`), so this is a role query and not a
 * class or test-id lookup: if the row stopped being a link -- a `<div
 * onClick>`, say -- this would fail, which is the correct outcome, because
 * that change would break keyboard users.
 */
export function issueRows(): HTMLElement[] {
  return within(issueList()).getAllByRole('link')
}

/**
 * The text of every rendered row, in order.
 *
 * The unit the pagination assertions are written in: text rather than DOM
 * nodes, because duplicate rows -- the failure those tests exist to catch --
 * show up in a list of strings as a repeated entry and in a list of nodes as
 * nothing at all.
 *
 * `textContent` and not the computed accessible name. They are close here but
 * not the same: the priority badge's visible "1 Urgent" is `aria-hidden` and
 * so is absent from the name while present in the text. Claims about a row's
 * *name* are made with `toHaveAccessibleName` at the point they are made;
 * this is for order and identity.
 */
export function issueRowTexts(): string[] {
  return issueRows().map((row) => row.textContent ?? '')
}
