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
    // `aria-label` first: the rail's icon-only controls have no text, and
    // `textContent` on one of those yields '' and then 'BUTTON', which
    // describes nothing a user would recognise.
    const label =
      active?.getAttribute('aria-label') ?? (active?.textContent ?? '').trim()

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

    /*
      Ordering rather than an exact list.

      The rail's contents are the shell's to change -- it grew a collapse
      toggle, a command affordance and an account menu -- and a test that
      pinned every stop would fail on every one of those without any of them
      being a regression. What must not change is the shape: the skip link
      first, the global create action before the list it files into, and the
      whole of the chrome before the first row.
    */
    /*
      The budget is a bound on the walk, not a claim about the rail's size.
      It was 16, which was one more thing pinned than the comment above
      intends: the rail grew twelve destinations (triage, initiatives,
      roadmap, documents, saved views, favourites, analytics, releases,
      environments, label groups, templates) and the walk stopped inside the
      navigation, so `firstRowAt` was -1 and the failure read as a tab-order
      regression rather than as a longer menu. Raised with headroom so the
      next section to land does not re-fail it; the assertions below are still
      about ORDER, and none of them cares how many stops precede the row.
    */
    const path = await tabPath(user, 40)

    // The skip link is first, which is the only position it works from.
    expect(path[0]).toBe('Skip to main content')

    const createAt = path.indexOf('New issue')
    const firstRowAt = path.findIndex((entry) => entry.includes('Alpha'))

    expect(createAt).toBeGreaterThan(0)
    expect(firstRowAt).toBeGreaterThan(createAt)

    // The command affordance is disabled, so no keyboard user can land on it
    // and wonder why pressing it does nothing.
    expect(path.some((entry) => entry.includes('Command palette'))).toBe(false)
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

  it('names every indicator rather than leaving it a bare glyph', async () => {
    await renderPopulatedList()

    /*
      The priority and status glyphs are `role="img"` with a name on the
      wrapper, so each is one opaque graphic with exactly one announcement --
      not a stack of unlabelled `<path>`s, and not two fragments a reader has
      to assemble. The names are this frontend's convention for an integer the
      API attaches no names to (see lib/priority.ts); the composer still says
      so in words.
    */
    const [alpha, bravo] = within(main()).getAllByRole('link')

    expect(alpha).toHaveAccessibleName(/Priority: Urgent/)
    expect(bravo).toHaveAccessibleName(/Priority: No priority/)

    // And the identifier, which is the name the issue has outside the
    // product, is in the row's name too -- it is what a person says out loud.
    expect(alpha).toHaveAccessibleName(/ENG-1/)
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
