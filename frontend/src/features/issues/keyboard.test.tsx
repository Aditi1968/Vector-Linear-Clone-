import { fireEvent, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { issueDetail, issueDetailData, issueListData, issueRow } from '../../test/factories'
import { renderApp } from '../../test/render'

/**
 * The `C` shortcut, and the one test that matters more than the rest.
 *
 * Proving that `C` opens the composer is easy and nearly worthless on its
 * own: the interesting property of a single-letter shortcut is everything it
 * must *not* do. It must not fire while the user is typing, and it must not
 * steal a platform chord. Both are silent when broken -- the first shows up
 * as a composer appearing while someone writes the word "critical", the
 * second as Ctrl+C not copying.
 */

/** Elements mounted outside React, cleaned up per test. */
const strays: HTMLElement[] = []

afterEach(() => {
  for (const stray of strays.splice(0)) {
    stray.remove()
  }
})

/**
 * Put a focused element on the page that the issue list does not own.
 *
 * The list screen renders no text field of its own today (search is a
 * disabled button, and the composer's fields only exist once it is open), so
 * the guard cannot be reached through the application's own markup while the
 * composer is closed. What the guard actually inspects is `event.target`, and
 * a real focused `<input>` in the same document produces exactly that -- so
 * this reproduces the condition faithfully rather than approximating it.
 */
function focusOutsideTheApp<T extends HTMLElement>(element: T): T {
  document.body.append(element)
  strays.push(element)
  element.focus()

  return element
}

async function renderList(): Promise<ReturnType<typeof renderApp>> {
  const view = renderApp()

  await view.link.resolve('IssueList', {
    data: issueListData([issueRow(1, { title: 'Alpha' })]),
  })

  return view
}

function composerIsOpen(): boolean {
  return screen.queryByRole('heading', { name: 'New issue' }) !== null
}

describe('the C shortcut', () => {
  it('opens the composer', async () => {
    const { user } = await renderList()

    expect(composerIsOpen()).toBe(false)

    await user.keyboard('c')

    expect(composerIsOpen()).toBe(true)
    // Focus lands in the field the user is about to fill.
    expect(screen.getByRole('textbox', { name: 'Title' })).toHaveFocus()
  })

  it('opens the composer for a capital C', async () => {
    const { user } = await renderList()

    // Shift+c is how a capital C arrives, and it is still ours.
    await user.keyboard('{Shift>}C{/Shift}')

    expect(composerIsOpen()).toBe(true)
  })

  it.each([
    ['Control', '{Control>}c{/Control}'],
    ['Meta', '{Meta>}c{/Meta}'],
    ['Alt', '{Alt>}c{/Alt}'],
  ])('leaves %s+C to the platform', async (_modifier, keys) => {
    const { user } = await renderList()

    await user.keyboard(keys)

    expect(composerIsOpen()).toBe(false)
  })

  describe('does not fire while the user is typing', () => {
    it('in a text input', async () => {
      const { user } = await renderList()

      const input = focusOutsideTheApp(document.createElement('input'))
      input.type = 'text'

      await user.keyboard('critical')

      expect(composerIsOpen()).toBe(false)
      // And the keystrokes went where the user aimed them, rather than being
      // swallowed by a shortcut that decided not to act.
      expect(input).toHaveValue('critical')
    })

    it('in a textarea', async () => {
      const { user } = await renderList()

      const textarea = focusOutsideTheApp(document.createElement('textarea'))

      await user.keyboard('cannot reproduce')

      expect(composerIsOpen()).toBe(false)
      expect(textarea).toHaveValue('cannot reproduce')
    })

    it('in a select, where a letter jumps to a matching option', async () => {
      const { user } = await renderList()

      const select = focusOutsideTheApp(document.createElement('select'))
      const option = document.createElement('option')
      option.textContent = 'Critical'
      select.append(option)

      await user.keyboard('c')

      expect(composerIsOpen()).toBe(false)
    })

    it('in a contenteditable element', async () => {
      await renderList()

      const editor = focusOutsideTheApp(document.createElement('div'))
      editor.setAttribute('contenteditable', 'true')

      /*
        jsdom leaves `isContentEditable` undefined however the attribute or
        property is set, so it is defined here to stand in for the browser.
        The shim is on the *environment*; the guard in
        `features/issues/lib/keyboard.ts` and the list screen's handler both
        run unmodified.

        `fireEvent` rather than `userEvent` for the same reason: jsdom will
        not route typed characters into an element it does not consider
        editable, and what this test is about is the `keydown` reaching the
        window listener with an editable target.
      */
      Object.defineProperty(editor, 'isContentEditable', { value: true })

      fireEvent.keyDown(editor, { key: 'c' })

      expect(composerIsOpen()).toBe(false)
    })
  })

  it('does not open a second composer while one is open', async () => {
    const { user } = await renderList()

    await user.keyboard('c')
    expect(screen.getAllByRole('heading', { name: 'New issue' })).toHaveLength(1)

    /*
      A title containing the shortcut's letter as a word on its own, not just
      inside one. `critical` alone would pass even against a guard that only
      ignored letters mid-word; a bare `c` between spaces is the keystroke
      that is indistinguishable from the shortcut except for where focus is.
    */
    await user.keyboard('critical c bug')

    expect(screen.getByRole('textbox', { name: 'Title' })).toHaveValue('critical c bug')
    expect(screen.getAllByRole('heading', { name: 'New issue' })).toHaveLength(1)
  })

  it('stops working once the screen that owns it is gone', async () => {
    const { link, user } = await renderList()

    await user.click(screen.getByRole('link', { name: /Alpha/ }))
    await link.resolve('IssueDetail', {
      data: issueDetailData(issueDetail(1, { title: 'Alpha' })),
    })

    await user.keyboard('c')

    // The listener is added while the list is mounted and removed with it.
    // A shortcut that outlived its screen would open a composer over a page
    // that has none.
    expect(composerIsOpen()).toBe(false)
  })
})

describe('Escape', () => {
  it('dismisses the composer', async () => {
    const { user } = await renderList()

    await user.keyboard('c')
    expect(composerIsOpen()).toBe(true)

    await user.keyboard('{Escape}')

    expect(composerIsOpen()).toBe(false)
  })

  it('dismisses the composer from inside a field', async () => {
    const { user } = await renderList()

    await user.keyboard('c')
    await user.type(screen.getByRole('textbox', { name: 'Title' }), 'half a thought')

    // Deliberately not subject to the typing guard: Escape while typing in
    // the composer is exactly the case it has to handle.
    await user.keyboard('{Escape}')

    expect(composerIsOpen()).toBe(false)
  })

  it('is left alone when there is nothing to dismiss', async () => {
    await renderList()

    const escape = new KeyboardEvent('keydown', {
      key: 'Escape',
      bubbles: true,
      cancelable: true,
    })
    window.dispatchEvent(escape)

    // Not consumed, so it can still reach whatever the browser or a future
    // dialog owns.
    expect(escape.defaultPrevented).toBe(false)
    expect(composerIsOpen()).toBe(false)
  })
})
