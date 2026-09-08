import { screen, within } from '@testing-library/react'
import { expect } from 'vitest'

import { main } from './render'

/**
 * The accessibility properties every screen has to hold, asserted once.
 *
 * `features/issues/accessibility.test.tsx` is the long-form version and says
 * why there is no axe pass: Testing Library's role and name queries fail
 * naturally when accessibility regresses, so the suite is the check. This
 * module is the short form -- the three properties that are cheap to state,
 * easy to break, and not exercised incidentally by a feature's own tests.
 *
 * It exists because it found real bugs. A row button whose name collided with
 * the rail's ("Accept", "New issue") and two controls in one form both
 * labelled "Team" both rendered correctly and both made the screen
 * unnavigable by voice or by screen-reader name lookup -- neither is visible
 * in a screenshot, and neither fails any other assertion.
 */

/**
 * Every button on the page can be named.
 *
 * An icon-only button with no name is announced as "button" and is unusable
 * with a screen reader, while looking completely correct.
 */
export function expectEveryButtonNamed(): void {
  const buttons = screen.getAllByRole('button')

  expect(buttons.length).toBeGreaterThan(0)

  for (const button of buttons) {
    expect(button).toHaveAccessibleName()
  }
}

/**
 * No two buttons anywhere on the page share an accessible name.
 *
 * Document-wide rather than scoped to `main`, because the collision that
 * matters most is between a screen's controls and the shell's: "activate the
 * one called X" is ambiguous exactly when two things are called X, and the
 * rail is on every screen.
 *
 * The name is approximated as `aria-label ?? textContent` rather than
 * computed properly. `dom-accessibility-api` would compute it exactly, and it
 * is present only as a transitive dependency of Testing Library -- importing
 * it here would be relying on hoisting, and declaring it would be adding a
 * dependency to catch a case (`aria-labelledby` on a button) that nothing in
 * this product uses. The approximation is exact for every button here and
 * catches both bugs this function was written for.
 */
export function expectNoDuplicateButtonNames(): void {
  const seen = new Map<string, number>()

  for (const button of screen.getAllByRole('button')) {
    const name = button.getAttribute('aria-label') ?? (button.textContent ?? '').trim()

    seen.set(name, (seen.get(name) ?? 0) + 1)
  }

  const duplicates = [...seen.entries()]
    .filter(([, count]) => count > 1)
    .map(([name]) => name)

  expect(duplicates).toEqual([])
}

/**
 * Exactly one `<h1>`, and it is the page's title.
 *
 * The outline is what a screen-reader user navigates a page by. Two `<h1>`s
 * say the page is two documents; none says it is a fragment.
 */
export function expectOneFirstLevelHeading(title: string): void {
  expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1)
  expect(screen.getByRole('heading', { level: 1, name: title })).toBeInTheDocument()
}

/**
 * The content's own headings start at level 2 and skip no level.
 *
 * A jump from `<h1>` to `<h3>` reads as a missing section rather than as a
 * subsection, which is the whole reason the levels are ordered at all.
 */
export function expectHeadingLevelsUnbroken(): void {
  const levels = within(main())
    .getAllByRole('heading')
    .map((heading) => Number(heading.tagName.slice(1)))

  let previous = 1

  for (const level of levels) {
    expect(level).toBeLessThanOrEqual(previous + 1)
    previous = level
  }
}
