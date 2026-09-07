import { fireEvent, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { WORKSPACE_SLUG } from '../../test/factories'
import { renderApp } from '../../test/render'

/**
 * The command palette, tested through the real shell.
 *
 * What is worth asserting here is not that a dialog rendered. It is the
 * handful of properties that are silent when they break:
 *
 *   - the chord does not fire while somebody is typing a `k` into a field;
 *   - focus goes into the palette, and comes back out to whatever opened it;
 *   - Escape closes it.
 *
 * The chord is dispatched on `window` rather than typed through `userEvent`,
 * because what the guard actually inspects is `event.target` -- and a real
 * focused element in the same document is the only faithful way to produce
 * one.
 */

/** Elements mounted outside React, cleaned up per test. */
const strays: HTMLElement[] = []

afterEach(() => {
  for (const stray of strays.splice(0)) {
    stray.remove()
  }
})

function focusOutsideTheApp<T extends HTMLElement>(element: T): T {
  document.body.append(element)
  strays.push(element)
  element.focus()

  return element
}

/**
 * The chord, as this platform spells it.
 *
 * jsdom's user agent reports neither a Mac nor iOS, so the shell resolves
 * `COMMAND_CHORD` to `['Ctrl', 'K']` and the binding matches `ctrlKey`. The
 * test asserts the behaviour of the platform it runs on rather than
 * hardcoding a second opinion about which platform that is.
 */
function pressChord(target: Window | Element = window): void {
  fireEvent.keyDown(target, { key: 'k', ctrlKey: true })
}

function palette(): HTMLElement | null {
  return screen.queryByRole('dialog', { name: 'Command palette' })
}

/**
 * The query field.
 *
 * `combobox` rather than `textbox`: the role is what tells a screen reader
 * that this field has a list attached and that the arrow keys mean something
 * in it, so querying by it is also an assertion that the pattern is intact.
 */
function paletteInput(): HTMLElement {
  return screen.getByRole('combobox', {
    name: 'Search issues and projects, or run a command',
  })
}

async function renderShell(): Promise<ReturnType<typeof renderApp>> {
  const view = renderApp()

  await view.link.idle()

  return view
}

describe('the command palette chord', () => {
  it('opens the palette', async () => {
    await renderShell()

    expect(palette()).toBeNull()

    pressChord()

    expect(palette()).toBeInTheDocument()
  })

  it('closes it again, so the chord is a toggle', async () => {
    await renderShell()

    pressChord()
    pressChord()

    expect(palette()).toBeNull()
  })

  /**
   * The test that matters most in this file.
   *
   * A palette bound to a chord is safe from the typing problem that makes a
   * single-letter shortcut dangerous -- Ctrl+K types nothing -- but the
   * *unmodified* key must never be taken. `k` alone is a character somebody
   * is entering into an issue title, and a palette that opened on it would
   * eat the letter and the sentence around it.
   */
  it('does not open on the key without its modifier, wherever focus is', async () => {
    await renderShell()

    fireEvent.keyDown(window, { key: 'k' })
    expect(palette()).toBeNull()

    const field = focusOutsideTheApp(document.createElement('input'))
    fireEvent.keyDown(field, { key: 'k' })

    expect(palette()).toBeNull()
  })

  it.each([
    ['an input', 'input'],
    ['a textarea', 'textarea'],
  ] as const)('still opens from inside %s', async (_label, tagName) => {
    await renderShell()

    const field = focusOutsideTheApp(document.createElement(tagName))
    pressChord(field)

    /*
      Deliberately the opposite expectation from the single-key shortcuts.
      A modified chord is not text entry, and a palette that refused to open
      because the caret was in the composer would be broken in exactly the
      moment somebody reaches for it.
    */
    expect(palette()).toBeInTheDocument()
  })

  it('leaves a chord with the wrong modifier alone', async () => {
    await renderShell()

    // Ctrl+Alt is how AltGr arrives on a Windows layout: taking it would
    // steal a character from anyone whose keyboard puts one there.
    fireEvent.keyDown(window, { key: 'k', ctrlKey: true, altKey: true })
    expect(palette()).toBeNull()

    // Cmd+K on a machine the shell spelled the chord for Ctrl on.
    fireEvent.keyDown(window, { key: 'k', metaKey: true })
    expect(palette()).toBeNull()
  })
})

describe('command palette focus', () => {
  it('moves focus into the query field on open', async () => {
    await renderShell()

    pressChord()

    expect(paletteInput()).toHaveFocus()
  })

  /**
   * Focus return is the browser's job on a modal `<dialog>` and jsdom does
   * not implement `showModal`, so this is testable only because the component
   * does it explicitly -- which is also what makes it work in the degraded
   * path `Dialog` falls back to when `showModal` is missing.
   */
  it('returns focus to the control that opened it', async () => {
    const { user } = await renderShell()

    const command = screen.getByRole('button', { name: 'Command' })
    await user.click(command)

    expect(paletteInput()).toHaveFocus()

    await user.keyboard('{Escape}')

    expect(palette()).toBeNull()
    expect(command).toHaveFocus()
  })

  it('starts empty every time, rather than showing the last query', async () => {
    const { user } = await renderShell()

    pressChord()
    await user.type(paletteInput(), 'stale')
    expect(paletteInput()).toHaveValue('stale')

    pressChord()
    pressChord()

    expect(paletteInput()).toHaveValue('')
  })
})

/** The option the combobox currently points at, by its accessible name. */
function activeOptionName(): string {
  const activeId = paletteInput().getAttribute('aria-activedescendant')

  expect(activeId).not.toBeNull()

  const option = document.getElementById(activeId ?? '')

  expect(option).not.toBeNull()
  expect(option).toHaveAttribute('role', 'option')
  expect(option).toHaveAttribute('aria-selected', 'true')

  return option?.textContent ?? ''
}

function optionNames(): string[] {
  return within(screen.getByRole('listbox')).getAllByRole('option').map(
    (option) => option.textContent ?? '',
  )
}

describe('moving through the palette', () => {
  it('names the cursor through aria-activedescendant, starting at the top', async () => {
    await renderShell()

    pressChord()

    const input = paletteInput()

    // The combobox pattern, spelled out: the field keeps focus, and the
    // option it points at is the one Enter will run.
    expect(input).toHaveAttribute('role', 'combobox')
    expect(input).toHaveAttribute('aria-expanded', 'true')
    expect(input).toHaveAttribute('aria-controls', screen.getByRole('listbox').id)

    expect(activeOptionName()).toBe('My Issues')
  })

  it('moves down and up with the arrow keys, and wraps at both ends', async () => {
    const { user } = await renderShell()

    pressChord()

    await user.keyboard('{ArrowDown}')
    expect(activeOptionName()).toBe('Inbox')

    await user.keyboard('{ArrowUp}')
    expect(activeOptionName()).toBe('My Issues')

    // Up from the first reaches the last, which is the fastest route to it.
    await user.keyboard('{ArrowUp}')
    expect(activeOptionName()).toBe(optionNames().at(-1))
  })

  it('keeps focus in the query field while the cursor moves', async () => {
    const { user } = await renderShell()

    pressChord()
    await user.keyboard('{ArrowDown}{ArrowDown}')

    // The whole reason for `aria-activedescendant`: the next character the
    // user types has to land in the field, not on an option.
    expect(paletteInput()).toHaveFocus()
  })

  it('navigates on Enter, and closes', async () => {
    const { user, currentPath } = await renderShell()

    pressChord()
    await user.keyboard('{ArrowDown}{Enter}')

    expect(currentPath()).toBe(`/${WORKSPACE_SLUG}/inbox`)
    expect(palette()).toBeNull()
  })

  it('navigates on a click', async () => {
    const { user, currentPath } = await renderShell()

    pressChord()
    await user.click(screen.getByRole('option', { name: 'Settings' }))

    expect(currentPath()).toBe(`/${WORKSPACE_SLUG}/settings`)
  })

  it('filters the commands, and says so when nothing matches', async () => {
    const { user } = await renderShell()

    pressChord()
    await user.type(paletteInput(), 'sett')

    expect(optionNames()).toEqual(['Settings'])
    expect(activeOptionName()).toBe('Settings')

    await user.type(paletteInput(), 'zzz')

    expect(screen.queryByRole('listbox')).toBeNull()
    expect(screen.getByText('No matches')).toBeInTheDocument()

    // Enter with nothing to run must not navigate or close.
    await user.keyboard('{Enter}')
    expect(palette()).toBeInTheDocument()
  })

  it('sends every command to a workspace-scoped URL', async () => {
    const { user, currentPath } = await renderShell()

    pressChord()
    await user.click(screen.getByRole('option', { name: 'Cycles' }))

    // Built from the path helpers, so the workspace segment is there without
    // any component writing one.
    expect(currentPath()).toBe(`/${WORKSPACE_SLUG}/cycles`)
  })
})
