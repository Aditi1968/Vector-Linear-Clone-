import { screen, within } from '@testing-library/react'
import type { UserEvent } from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import { issueListData, issueRow } from '../../test/factories'
import { main, renderApp } from '../../test/render'

/**
 * Accessibility, asserted through the queries the rest of this suite already
 * uses.
 *
 * There is no separate axe pass here on purpose. Testing Library's role and
 * name queries fail *naturally* when accessibility regresses -- a button that
 * loses its label stops being findable by name, a row that stops being a link
 * stops being findable as one -- so the whole suite is the check, and these
 * tests cover the properties the other files do not happen to exercise.
 */

async function renderPopulatedList(): Promise<ReturnType<typeof renderApp>> {
  const view = renderApp()

  await view.link.resolve('IssueList', {
    data: issueListData([
      issueRow(1, { title: 'Alpha', priority: 1 }),
      issueRow(2, { title: 'Bravo', priority: 0 }),
    ]),
  })

  return view
}

/** What Tab reaches, in order, described the way a user would recognise it. */
async function tabPath(user: UserEvent, steps: number): Promise<string[]> {
  const path: string[] = []

  for (let step = 0; step < steps; step += 1) {
    await user.tab()

    const active = document.activeElement
    const label = active === null ? '' : (active.textContent ?? '').trim()

    path.push(label.length > 0 ? label : (active?.tagName ?? '(none)'))
  }

  return path
}

describe('accessibility', () => {
  it('gives every button an accessible name', async () => {
    const { user } = await renderPopulatedList()

    // With the composer open, so the form's buttons are included.
    await user.keyboard('c')

    const buttons = screen.getAllByRole('button')
    expect(buttons.length).toBeGreaterThan(0)

    for (const button of buttons) {
      // An icon-only button with no name is announced as "button" and is
      // unusable with a screen reader -- and it looks completely correct.
      expect(button).toHaveAccessibleName()
    }
  })

  it('labels every control in the composer', async () => {
    const { user } = await renderPopulatedList()

    await user.keyboard('c')

    expect(screen.getByRole('textbox', { name: 'Title' })).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: /^Description/ })).toBeInTheDocument()
    expect(screen.getByRole('combobox', { name: 'Priority' })).toBeInTheDocument()

    // Every control's help text is in its accessible description rather than
    // only next to it on screen.
    expect(screen.getByRole('combobox', { name: 'Priority' })).toHaveAccessibleDescription(
      /convention/,
    )
  })

  it('puts the chrome before the content in the tab order, and skips what cannot be used', async () => {
    const { user } = await renderPopulatedList()

    const path = await tabPath(user, 6)

    // The skip link is first, which is the only position it works from.
    expect(path[0]).toBe('Skip to main content')
    expect(path[1]).toBe('New issue')

    // Then the whole of the sidebar's navigation, in its rendered order, and
    // only then the content. Asserted as a relative order rather than as
    // fixed indices: the nav is a list that grows as surfaces become real
    // (Projects and Cycles joined Issues when their resolvers landed), and
    // the property this test is about -- chrome before content -- does not
    // depend on how many entries it has.
    const firstContent = path.findIndex((entry) => entry.includes('Alpha'))
    expect(firstContent).toBeGreaterThan(-1)
    expect(path.slice(2, firstContent)).toEqual(['Issues', 'Projects', 'Cycles'])

    // The search affordance is disabled, so no keyboard user can land on it
    // and wonder why nothing happens.
    expect(path.some((entry) => entry.includes('Search'))).toBe(false)
  })

  it('moves focus through the composer in the order it is read', async () => {
    const { user } = await renderPopulatedList()

    await user.keyboard('c')

    // The composer takes focus on open, so tabbing continues from the title.
    expect(screen.getByRole('textbox', { name: 'Title' })).toHaveFocus()

    await user.tab()
    expect(screen.getByRole('textbox', { name: /^Description/ })).toHaveFocus()

    await user.tab()
    expect(screen.getByRole('combobox', { name: 'Priority' })).toHaveFocus()

    await user.tab()
    expect(screen.getByRole('button', { name: 'Create issue' })).toHaveFocus()

    await user.tab()
    expect(screen.getByRole('button', { name: 'Cancel' })).toHaveFocus()
  })

  it('makes the main region focusable so the skip link can reach it', async () => {
    await renderPopulatedList()

    // Without this, following the skip link scrolls but leaves focus on the
    // link, so the next Tab lands back at the top of the sidebar -- the exact
    // loop the link exists to break.
    expect(main()).toHaveAttribute('tabindex', '-1')
  })

  it('announces a priority as a sentence rather than two fragments', async () => {
    await renderPopulatedList()

    // The visible "1 Urgent" is `aria-hidden`; one hidden string carries the
    // whole thing, disclaimer included, so nobody reads "High" off a screen
    // and concludes the schema has a `High`.
    expect(
      within(main()).getByText('Priority 1, labelled Urgent by this app'),
    ).toBeInTheDocument()
    expect(
      within(main()).getByText('Priority 0, labelled No priority by this app'),
    ).toBeInTheDocument()
  })

  it('exposes the rows as a list of links', async () => {
    await renderPopulatedList()

    const list = within(main()).getByRole('list')

    // `role="list"` is stated in the markup because Safari drops list
    // semantics when the markers are removed; asserting it here is what keeps
    // that from being deleted as redundant.
    expect(list).toHaveAttribute('role', 'list')
    expect(within(list).getAllByRole('listitem')).toHaveLength(2)

    for (const row of within(list).getAllByRole('link')) {
      // A real link, so Tab reaches it, Enter follows it, and middle-click
      // opens it in a new tab -- none of which a `div role="link"` gets.
      expect(row).toHaveAttribute('href')
      expect(row).toHaveAccessibleName()
    }
  })

  it('gives each screen exactly one first-level heading', async () => {
    const { user } = await renderPopulatedList()

    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1)
    expect(screen.getByRole('heading', { level: 1 })).toHaveAccessibleName('Issues')

    await user.keyboard('c')

    // A section heading inside the page starts at level 2, and the page's
    // only `<h1>` stays the one `PageHeader` renders.
    expect(
      screen.getByRole('heading', { level: 2, name: 'New issue' }),
    ).toBeInTheDocument()
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1)
  })
})
