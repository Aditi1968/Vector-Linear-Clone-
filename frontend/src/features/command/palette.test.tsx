import { fireEvent, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

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

function paletteInput(): HTMLElement {
  return screen.getByRole('textbox', {
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
