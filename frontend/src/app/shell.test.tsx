import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import {
  WORKSPACE_SLUG,
  issueListData,
  issueRow,
  membership,
  shellSidebarData,
  workspaceShellData,
} from '../test/factories'
import { main, renderApp } from '../test/render'
import type { RenderAppOptions, RenderAppResult } from '../test/render'

// A URL inside the workspace that matches no page. `/somewhere-else` is not
// one any more: the first segment is the workspace slug, so an unknown
// top-level path is a workspace nobody has rather than a missing page.
const UNKNOWN_PATH = `/${WORKSPACE_SLUG}/somewhere-else`

const ENG = { id: '00000000-0000-4000-8000-00000000ee01', key: 'ENG', name: 'Engineering' }

/**
 * Mount, and let the shell's own query land.
 *
 * `renderApp` is synchronous and the shell is not: it will not render a rail,
 * a page or a redirect until `WorkspaceShell` has answered, and Apollo
 * delivers even a synchronous link result on a scheduler rather than inside
 * the mounting render. Every test here is about what the shell does *with*
 * that answer, so waiting for it once in a helper beats fifteen copies of the
 * same `await` -- and the one test that is about the state before it arrives
 * calls `renderApp` directly.
 */
async function renderShell(options: RenderAppOptions = {}): Promise<RenderAppResult> {
  const view = renderApp(options)

  await view.link.idle()

  return view
}

/**
 * The application shell.
 *
 * What is worth asserting about chrome is not that it rendered but that it is
 * *navigable*, *honest* and *safe*: landmarks a screen reader can jump
 * between, navigation that changes the URL, state that survives a reload, and
 * -- the one that is a security property rather than a usability one -- a
 * workspace slug in the URL that is treated as a route parameter and never as
 * a claim.
 */
describe('application shell', () => {
  it('renders the banner, navigation and main landmarks', async () => {
    const { link } = await renderShell()

    await link.resolve('IssueList', { data: issueListData([issueRow(1)]) })

    /*
      Scoped to elements outside `main` on purpose.

      `PageHeader` also renders a `<header>`, and per HTML-AAM a `<header>`
      descended from `<main>` is a plain generic element, not a second banner
      -- so a browser exposes exactly one banner here. Testing Library's role
      mapping does not implement that ancestor rule and reports both, which is
      a limitation of the query and not a fault in the shell. Filtering by
      containment asserts the thing that is actually true of the page.
    */
    const shellBanner = screen
      .getAllByRole('banner')
      .find((banner) => !main().contains(banner))

    expect(shellBanner).toBeDefined()
    expect(shellBanner).toContainElement(
      screen.getByRole('navigation', { name: 'Main' }),
    )

    // Exactly one `main`. Two would mean a page rendered its own inside the
    // shell's, which is invalid and defeats landmark navigation.
    expect(screen.getAllByRole('main')).toHaveLength(1)
  })

  it('offers a skip link that points at the main region', async () => {
    const { link } = await renderShell()

    await link.resolve('IssueList', { data: issueListData([issueRow(1)]) })

    const skipLink = screen.getByRole('link', { name: 'Skip to main content' })

    // The skip link must be the first thing Tab reaches, or it cannot do its
    // job -- a skip link the user has to tab past the rail to reach is worse
    // than none.
    expect(skipLink.compareDocumentPosition(main())).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    )

    const href = skipLink.getAttribute('href')
    expect(href).not.toBeNull()
    expect(main().id).toBe(href?.slice(1))
  })

  /* ---------------------------------------------------------------- */
  /* Resolving the workspace                                           */
  /* ---------------------------------------------------------------- */

  describe('the workspace in the URL', () => {
    // Synchronous, and the only test here that is. It asserts the state
    // *before* the shell query lands, so it must not flush it first.
    it('shows skeletons rather than a spinner while it is being resolved', async () => {
      renderApp({ shell: null })

      // The shell query gates every authenticated screen, so this is what the
      // user sees on every cold load. A skeleton says "here is what is
      // coming"; a spinner over the whole application says "wait".
      //
      // Awaited rather than asserted synchronously: `RequireAuth` resolves the
      // viewer before the shell mounts, so at the end of `render` the screen
      // is still the guard's. The state under test is the one after that.
      expect(await screen.findByText('Loading your workspaces')).toBeInTheDocument()
      expect(screen.queryByRole('navigation', { name: 'Main' })).not.toBeInTheDocument()
    })

    it('reports a failure without a rail and without the error itself', async () => {
      const { link } = renderApp({ shell: null })

      await link.fail('WorkspaceShell', new Error('ECONNREFUSED 10.0.0.4:5432'))

      const alert = screen.getByRole('alert')
      expect(alert).toHaveTextContent('Could not load your workspaces')

      // Not the exception. The user can do nothing with a transport error,
      // and the shell has no way to tell a public backend message from an
      // internal one.
      expect(document.body.textContent).not.toContain('ECONNREFUSED')

      // No half-drawn rail: a rail is built from a workspace, and there is
      // no workspace here.
      expect(screen.queryByRole('navigation', { name: 'Main' })).not.toBeInTheDocument()
    })

    it('sends a signed-in user with no workspace to onboarding', async () => {
      const { currentPath } = await renderShell({ shell: workspaceShellData([]) })

      // A real state -- an account created but not invited anywhere -- and
      // exactly one thing to do next. Matched as a prefix because onboarding
      // owns what happens after the redirect: it sends the user on to
      // whichever of its steps is unfinished, and which one that is is not
      // the shell's business.
      expect(currentPath()).toMatch(/^\/onboarding/)
    })

    /**
     * The security-shaped one.
     *
     * The slug is a route parameter. A viewer who is not a member of it must
     * get the same screen whether the workspace exists or not, and must not
     * be shown a shell built around it.
     */
    it('renders a clean not-found for a slug the viewer has no membership for', async () => {
      await renderShell({
        initialPath: '/somebody-elses-workspace/issues',
        shell: workspaceShellData([membership(WORKSPACE_SLUG, { name: 'Acme' })]),
      })

      expect(
        screen.getByRole('heading', { level: 1, name: 'Workspace not found' }),
      ).toBeInTheDocument()

      // Not a half-rendered shell. No rail, no create action, no page.
      expect(screen.queryByRole('navigation', { name: 'Main' })).not.toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'New issue' })).not.toBeInTheDocument()

      // Exactly one `main`, and it is this screen's.
      expect(screen.getAllByRole('main')).toHaveLength(1)
    })

    it('never asks the server about a slug the viewer has no membership for', async () => {
      const { link } = await renderShell({
        initialPath: '/somebody-elses-workspace/issues',
        shell: workspaceShellData([membership(WORKSPACE_SLUG, { name: 'Acme' })]),
      })

      /*
        The strongest form of the guarantee, and the reason the shell resolves
        the slug against `myWorkspaces` instead of calling `myWorkspace(slug:)`.

        Nothing is sent naming this workspace, so the client cannot learn
        whether it exists -- there is no response to infer it from. The server
        would answer NOT_FOUND identically either way; this makes the question
        unasked rather than merely unanswerable.
      */
      const named = link.operations.filter((operation) =>
        JSON.stringify(operation.variables).includes('somebody-elses-workspace'),
      )

      expect(named).toEqual([])
    })
  })

  /* ---------------------------------------------------------------- */
  /* Workspace switching                                               */
  /* ---------------------------------------------------------------- */

  describe('the workspace switcher', () => {
    it('is not offered when the viewer belongs to one workspace', async () => {
      const { link } = await renderShell()

      await link.resolve('IssueList', { data: issueListData([issueRow(1)]) })

      // A menu of one entry is an affordance that promises something the
      // product cannot deliver, so the identity is a plain block instead.
      expect(
        screen.queryByRole('button', { name: /switch workspace/i }),
      ).not.toBeInTheDocument()
      expect(screen.getByText('Acme')).toBeInTheDocument()
    })

    it('navigates to another workspace the viewer belongs to', async () => {
      const { user, currentPath } = await renderShell({
        shell: workspaceShellData([
          membership(WORKSPACE_SLUG, { name: 'Acme' }),
          membership('globex', { name: 'Globex' }),
        ]),
      })

      // The trigger is named by the workspace it is showing, which is what a
      // user recognises it by.
      await user.click(screen.getByRole('button', { name: 'Acme' }))
      await user.click(screen.getByRole('menuitem', { name: 'Globex' }))

      // Actually navigates, and to the other workspace's own URL -- not to a
      // slug held in state somewhere, which is what would make the resulting
      // address unshareable.
      expect(currentPath()).toBe('/globex')
    })
  })

  /* ---------------------------------------------------------------- */
  /* Navigation                                                        */
  /* ---------------------------------------------------------------- */

  describe('the navigation', () => {
    it('lists the workspace’s real teams, each expandable to its own sections', async () => {
      const { user } = await renderShell({ sidebar: shellSidebarData([ENG]) })

      const nav = screen.getByRole('navigation', { name: 'Main' })
      const teams = await within(nav).findByRole('list', { name: 'Teams' })

      // The team comes from `teams(workspaceSlug:)`. There is no fixture team
      // anywhere in the shell, so a rail with a team in it is a rail the
      // server put one in.
      expect(within(teams).getByRole('link', { name: /Engineering/ })).toBeInTheDocument()

      // Collapsed until asked for, and the disclosure says which way it goes.
      expect(within(teams).queryByRole('link', { name: 'Cycles' })).not.toBeInTheDocument()

      await user.click(within(teams).getByRole('button', { name: 'Expand Engineering' }))

      expect(within(teams).getByRole('link', { name: 'Issues' })).toHaveAttribute(
        'href',
        `/${WORKSPACE_SLUG}/team/ENG/issues`,
      )
      // Workspace-level, unlike Issues beside it: `Cycle` carries no team in
      // the API, so the cycles screen picks its own team rather than reading
      // one from a URL it could never rebuild from a cycle.
      expect(within(teams).getByRole('link', { name: 'Cycles' })).toHaveAttribute(
        'href',
        `/${WORKSPACE_SLUG}/cycles`,
      )
    })

    it('badges the Inbox with the real unread count and omits it at zero', async () => {
      const { unmount } = await renderShell({ sidebar: shellSidebarData([], 3) })

      /*
        The name is pinned exactly, punctuation and all.

        `notificationUnreadCount` supplies the number; the word is what makes
        it mean something. The exact string matters because accessible names
        are built by concatenating each node's trimmed text: the obvious
        markup -- a visible "3" beside a hidden " unread" -- computes to
        "Inbox3unread", which is what this assertion caught. Matching loosely
        would have let that back in.
      */
      expect(
        await screen.findByRole('link', { name: 'Inbox, 3 unread' }),
      ).toBeInTheDocument()

      unmount()

      await renderShell({ sidebar: shellSidebarData([], 0) })

      // No empty pill claiming to be a count.
      expect(await screen.findByRole('link', { name: 'Inbox' })).toBeInTheDocument()
    })

    it('navigates to a workspace-scoped URL from another route', async () => {
      const { link, user, currentPath } = await renderShell({ initialPath: UNKNOWN_PATH })

      // The unknown route renders inside the shell rather than replacing it.
      const nav = screen.getByRole('navigation', { name: 'Main' })
      expect(currentPath()).toBe(UNKNOWN_PATH)

      await user.click(within(nav).getByRole('link', { name: 'All Issues' }))

      expect(currentPath()).toBe(`/${WORKSPACE_SLUG}/issues`)

      await link.resolve('IssueList', {
        data: issueListData([issueRow(1, { title: 'Reached the list' })]),
      })

      expect(await screen.findByText('Reached the list')).toBeInTheDocument()
    })

    it('marks All Issues as the current page while on a detail view', async () => {
      const { link } = await renderShell({
        initialPath: `/${WORKSPACE_SLUG}/issues/00000000-0000-4000-8000-000000000001`,
      })

      await link.idle()

      const nav = screen.getByRole('navigation', { name: 'Main' })

      // Prefix matching, on purpose: a detail view is inside that section, and
      // a rail that de-highlights there tells the user they left the section
      // they are still in.
      expect(within(nav).getByRole('link', { name: 'All Issues' })).toHaveAttribute(
        'aria-current',
        'page',
      )
    })
  })

  /* ---------------------------------------------------------------- */
  /* The rail's own state                                              */
  /* ---------------------------------------------------------------- */

  describe('collapsing the rail', () => {
    it('announces what the toggle will do and what it controls', async () => {
      const { user } = await renderShell()

      const toggle = screen.getByRole('button', { name: 'Collapse sidebar' })
      const nav = screen.getByRole('navigation', { name: 'Main' })

      expect(toggle).toHaveAttribute('aria-expanded', 'true')
      expect(toggle).toHaveAttribute('aria-controls', nav.id)

      await user.click(toggle)

      const expandToggle = screen.getByRole('button', { name: 'Expand sidebar' })
      expect(expandToggle).toHaveAttribute('aria-expanded', 'false')
    })

    it('remembers the choice across a reload', async () => {
      const first = await renderShell()

      await first.user.click(screen.getByRole('button', { name: 'Collapse sidebar' }))
      first.unmount()

      // A second mount is what a reload is, from the shell's point of view:
      // fresh React state, the same `localStorage`.
      await renderShell()

      expect(screen.getByRole('button', { name: 'Expand sidebar' })).toBeInTheDocument()
    })

    it('keeps every navigation label reachable while collapsed', async () => {
      const { user } = await renderShell()

      await user.click(screen.getByRole('button', { name: 'Collapse sidebar' }))

      // Labels are clipped by CSS, never removed from the DOM. Dropping them
      // would leave a screen-reader user with rows announced as bare "link".
      const nav = screen.getByRole('navigation', { name: 'Main' })
      expect(within(nav).getByRole('link', { name: 'My Issues' })).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'New issue' })).toBeInTheDocument()
    })
  })

  /* ---------------------------------------------------------------- */
  /* The account                                                       */
  /* ---------------------------------------------------------------- */

  describe('the account menu', () => {
    it('shows the signed-in user from `me` and offers a way out', async () => {
      const { user } = await renderShell()

      expect(screen.getByText('Ada Lovelace')).toBeInTheDocument()
      expect(screen.getByText('ada@example.com')).toBeInTheDocument()

      await user.click(screen.getByRole('button', { name: 'Account' }))

      expect(screen.getByRole('menuitem', { name: 'Sign out' })).toBeInTheDocument()
    })

    /*
      This replaces a test that asserted the theme control wrote `data-theme`
      onto `<html>`. It did, faithfully -- and nothing in the product read it.
      `tokens.css` pins `color-scheme: dark` on bare `:root` and declares no
      `prefers-color-scheme` block and no `:root[data-theme='light']` rule, so
      all three options rendered the same navy and the only observable effect
      of choosing one was that the segment moved.

      The old test passed the whole time. That is the point: it asserted the
      mechanism and never the outcome, so it certified a control a user could
      operate and could not affect. A control that visibly does nothing is
      worse than an absent one -- it teaches that this product's settings are
      decorative -- so the control is gone until there is a palette to switch
      to, and this asserts it stays gone.

      `preferences.ts` keeps `useTheme` and its storage: it is a correct
      implementation of persistence, `matchMedia` tracking and the
      system-versus-explicit distinction, all of which a light theme needs on
      the day it arrives, and deleting it would also discard the choice
      already stored in the browser of anyone who used the control.

      When an authenticated light palette lands, this test is what fails, and
      that failure is the reminder to delete it.
    */
    it('offers no appearance control while the product ships one palette', async () => {
      await renderShell()

      // By role and name rather than by component: this must fail for a theme
      // picker however it is built. A test pinned to `radiogroup` would pass
      // while a decorative `<select>` shipped beside it.
      for (const name of [/theme/i, /appearance/i, /dark mode/i]) {
        expect(screen.queryByRole('radiogroup', { name })).toBeNull()
        expect(screen.queryByRole('group', { name })).toBeNull()
        expect(screen.queryByRole('combobox', { name })).toBeNull()
        expect(screen.queryByRole('switch', { name })).toBeNull()
      }

      // And the option labels, since a control could be unlabelled and still
      // be operable. `Dark` is deliberately not among them: it is a plausible
      // word elsewhere in a product this colour, and a test that fails on the
      // wrong thing is worse than one fewer assertion.
      expect(screen.queryByRole('radio', { name: 'Auto' })).toBeNull()
      expect(screen.queryByRole('radio', { name: 'Light' })).toBeNull()

      // The attribute the removed control used to write. Absent because
      // nothing writes it now -- not because something cleared it.
      expect(document.documentElement.hasAttribute('data-theme')).toBe(false)
    })
  })

  /* ---------------------------------------------------------------- */
  /* Affordances that are honest about what they do                    */
  /* ---------------------------------------------------------------- */

  describe('the create-issue affordance', () => {
    it('is enabled on the issue list, where a composer exists', async () => {
      const { link } = await renderShell()

      await link.resolve('IssueList', { data: issueListData([issueRow(1)]) })

      expect(screen.getByRole('button', { name: 'New issue' })).toBeEnabled()
    })

    it('survives the double-invoked effects of a StrictMode mount', async () => {
      const { link } = await renderShell({ strictMode: true })

      await link.resolve('IssueList', { data: issueListData([issueRow(1)]) })

      /*
        Named for what it checks and no more.

        This does NOT exercise the identity check in
        `CreateIssueActionProvider`. StrictMode's order is register ->
        cleanup -> register, so the last thing to run is a registration and
        the slot ends up filled whether the cleanup checked identity or
        cleared unconditionally. The ordering the identity check actually
        guards -- register A, register B, then A's cleanup -- is driven
        directly in ./layout/createIssueAction.test.tsx.
      */
      expect(screen.getByRole('button', { name: 'New issue' })).toBeEnabled()
    })

    it('is disabled on a screen that cannot create issues', async () => {
      await renderShell({ initialPath: UNKNOWN_PATH })

      // Disabled rather than enabled-and-inert. A primary button that
      // swallows a click is a bug report, not a feature gap.
      expect(screen.getByRole('button', { name: 'New issue' })).toBeDisabled()
    })
  })

  it('links search at a real workspace-scoped URL', async () => {
    await renderShell({ initialPath: UNKNOWN_PATH })

    // A link, not a text input that swallows what is typed into it.
    expect(screen.getByRole('link', { name: 'Search' })).toHaveAttribute(
      'href',
      `/${WORKSPACE_SLUG}/search`,
    )
  })

  it('opens the command palette from the rail', async () => {
    const { user } = await renderShell({ initialPath: UNKNOWN_PATH })

    /*
      The accessible name is the visible word, so WCAG 2.5.3 (Label in Name)
      holds: "Command" is what is on screen and what a speech-input user can
      say. The chord reaches a screen reader through `aria-keyshortcuts`
      instead of being spelled into the name, which is what keeps the name
      from drifting as the hint changes shape.
    */
    const command = screen.getByRole('button', { name: 'Command' })

    expect(command).toHaveAttribute('aria-haspopup', 'dialog')
    expect(command).toHaveTextContent('K')

    await user.click(command)

    expect(screen.getByRole('dialog', { name: 'Command palette' })).toBeInTheDocument()
  })

  it('renders an unknown URL inside the shell, with one main landmark', async () => {
    const { currentPath } = await renderShell({ initialPath: UNKNOWN_PATH })

    // The URL that missed is kept, which is what makes a mistyped link
    // recoverable rather than a dead end.
    expect(currentPath()).toBe(UNKNOWN_PATH)
    expect(screen.getByRole('navigation', { name: 'Main' })).toBeInTheDocument()

    // The not-found screen renders a fragment and adds no `<main>` of its
    // own. A second one would give the document two "main" landmarks, and
    // the skip link would land in the wrong one.
    expect(screen.getAllByRole('main')).toHaveLength(1)

    expect(
      screen.getByRole('heading', { level: 1, name: 'Page not found' }),
    ).toBeInTheDocument()
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1)
  })
})
