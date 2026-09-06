import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { issueListData, issueRow } from '../test/factories'
import { main, renderApp } from '../test/render'

/**
 * The application shell.
 *
 * What is worth asserting about chrome is not that it rendered but that it is
 * *navigable*: landmarks a screen reader can jump between, a skip link that
 * reaches the content, navigation that changes the URL, and a create
 * affordance that is honest about whether it can do anything.
 */
describe('application shell', () => {
  it('renders the banner, navigation and main landmarks', async () => {
    const { link } = renderApp()

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
    const { link } = renderApp()

    await link.resolve('IssueList', { data: issueListData([issueRow(1)]) })

    const skipLink = screen.getByRole('link', { name: 'Skip to main content' })

    // The skip link must be the first thing Tab reaches, or it cannot do its
    // job -- a skip link the user has to tab past the sidebar to reach is
    // worse than none.
    expect(document.body.compareDocumentPosition(skipLink)).toBeDefined()
    expect(skipLink.compareDocumentPosition(main())).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    )

    const href = skipLink.getAttribute('href')
    expect(href).not.toBeNull()
    expect(main().id).toBe(href?.slice(1))
  })

  it('redirects the root URL to the issue list', async () => {
    const { link, currentPath } = renderApp({ initialPath: '/' })

    await link.resolve('IssueList', { data: issueListData([issueRow(1)]) })

    expect(currentPath()).toBe('/issues')
    expect(screen.getByRole('heading', { level: 1, name: 'Issues' })).toBeInTheDocument()
  })

  it('navigates to Issues from another route', async () => {
    const { link, user, currentPath } = renderApp({ initialPath: '/somewhere-else' })

    // The unknown route renders inside the shell rather than replacing it.
    expect(screen.getByRole('navigation', { name: 'Main' })).toBeInTheDocument()
    expect(currentPath()).toBe('/somewhere-else')

    const nav = screen.getByRole('navigation', { name: 'Main' })
    await user.click(within(nav).getByRole('link', { name: 'Issues' }))

    expect(currentPath()).toBe('/issues')

    await link.resolve('IssueList', {
      data: issueListData([issueRow(1, { title: 'Reached the list' })]),
    })

    expect(await screen.findByText('Reached the list')).toBeInTheDocument()
  })

  it('marks the Issues navigation item as the current page while on a detail view', async () => {
    const { link } = renderApp({
      initialPath: '/issues/00000000-0000-4000-8000-000000000001',
    })

    await link.idle()

    const nav = screen.getByRole('navigation', { name: 'Main' })

    // Prefix matching, on purpose: a detail view is inside Issues, and a
    // sidebar that de-highlights there tells the user they left the section
    // they are still in.
    expect(within(nav).getByRole('link', { name: 'Issues' })).toHaveAttribute(
      'aria-current',
      'page',
    )
  })

  describe('the create-issue affordance', () => {
    it('is enabled on the issue list, where a composer exists', async () => {
      const { link } = renderApp()

      await link.resolve('IssueList', { data: issueListData([issueRow(1)]) })

      expect(screen.getByRole('button', { name: 'New issue' })).toBeEnabled()
    })

    it('survives the double-invoked effects of a StrictMode mount', async () => {
      const { link } = renderApp({ strictMode: true })

      await link.resolve('IssueList', { data: issueListData([issueRow(1)]) })

      /*
        Named for what it checks and no more.

        This does NOT exercise the identity check in
        `CreateIssueActionProvider`. StrictMode's order is register ->
        cleanup -> register, so the last thing to run is a registration and
        the slot ends up filled whether the cleanup checked identity or
        cleared unconditionally. An earlier version of this test claimed to
        cover that ordering and did not, which is worse than no test: it
        stops anyone writing the real one.

        The ordering the identity check actually guards -- register A,
        register B, then A's cleanup -- is driven directly in
        ./layout/createIssueAction.test.tsx.

        What is left here is still worth asserting: the app really does mount
        inside `<StrictMode>` (src/main.tsx), and a registration effect that
        did not survive being invoked twice would leave the sidebar's primary
        action dead on the one screen that has a composer.
      */
      expect(screen.getByRole('button', { name: 'New issue' })).toBeEnabled()
    })

    it('is disabled on a screen that cannot create issues', () => {
      renderApp({ initialPath: '/somewhere-else' })

      // Disabled rather than enabled-and-inert. A primary button that
      // swallows a click is a bug report, not a feature gap.
      expect(screen.getByRole('button', { name: 'New issue' })).toBeDisabled()
    })
  })

  it('renders an unknown URL inside the shell, with one main landmark', () => {
    const { currentPath } = renderApp({ initialPath: '/somewhere-else' })

    // The URL that missed is kept, which is what makes a mistyped link
    // recoverable rather than a dead end.
    expect(currentPath()).toBe('/somewhere-else')
    expect(screen.getByRole('navigation', { name: 'Main' })).toBeInTheDocument()

    // The not-found screen renders a fragment and adds no `<main>` of its
    // own. A second one would give the document two "main" landmarks, and
    // the skip link would land in the wrong one.
    expect(screen.getAllByRole('main')).toHaveLength(1)

    expect(
      screen.getByRole('heading', { level: 1, name: 'Page not found' }),
    ).toBeInTheDocument()
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1)
    expect(screen.getByRole('link', { name: 'Back to issues' })).toBeInTheDocument()
  })

  it('does not present search as something that works', () => {
    renderApp({ initialPath: '/somewhere-else' })

    /*
      The accessible name is pinned exactly rather than matched loosely.

      It is one of four independent signals that this control does nothing --
      the others being the disabled state, the dashed-and-dimmed treatment,
      and the visible "Soon" badge -- and it is the only one of the four that
      reaches a screen-reader user. A `/^Search/` match would still pass with
      the `aria-label` deleted, because the visible text alone reads
      "Search Soon": the signal would be gone for exactly the people who
      cannot see the other three, and the test would not notice.

      The name begins with the visible word "Search" so that WCAG 2.5.3
      (Label in Name) holds -- a speech-input user saying "click Search"
      still matches this control.
    */
    const search = screen.getByRole('button', { name: 'Search — not available yet' })

    expect(search).toHaveAccessibleName('Search — not available yet')
    expect(search).toHaveTextContent('Soon')

    // A disabled button, not a text input: nothing about it should invite
    // typing, and nothing should let a keyboard user land on it and wonder.
    expect(search).toBeDisabled()
    expect(screen.queryByRole('searchbox')).not.toBeInTheDocument()
    expect(screen.queryByRole('textbox', { name: /search/i })).not.toBeInTheDocument()
  })
})
