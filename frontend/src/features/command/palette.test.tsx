import { fireEvent, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { WORKSPACE_SLUG, issueId, issueListData, issueRow } from '../../test/factories'
import { renderApp } from '../../test/render'
import type { CommandSearchQuery } from '../../generated/operations'

/**
 * A search response, typed against the real operation.
 *
 * Built here rather than added to `src/test/factories.ts`: the palette is the
 * only caller of this document, and a fixture in the shared file would be a
 * shape three other agents have to merge around for no benefit.
 */
function searchData(
  issues: { id: string; identifier: string; title: string }[] = [],
  projects: { id: string; name: string }[] = [],
): CommandSearchQuery {
  return {
    search: {
      __typename: 'SearchResults',
      issues: issues.map((issue) => ({ __typename: 'Issue', ...issue })),
      projects: projects.map((project) => ({ __typename: 'Project', ...project })),
    },
  }
}

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
    const { user, link } = await renderShell()

    pressChord()
    await user.type(paletteInput(), 'sett')
    await link.resolve('CommandSearch', { data: searchData() })

    expect(optionNames()).toEqual(['Settings'])
    expect(activeOptionName()).toBe('Settings')

    await user.type(paletteInput(), 'zzz')
    await link.resolve('CommandSearch', { data: searchData() })

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

describe('searching the workspace from the palette', () => {
  it('asks once for the last thing typed, not once per keystroke', async () => {
    const { link } = await renderShell()

    pressChord()

    const input = paletteInput()

    /*
      `fireEvent.change` rather than `user.type`, and that is not a shortcut.
      The debounce is a real 200ms timer, so a test that typed with a
      keystroke delay would be asserting that the machine running it is fast
      -- three synchronous changes are unambiguously one burst.
    */
    fireEvent.change(input, { target: { value: 'a' } })
    fireEvent.change(input, { target: { value: 'au' } })
    fireEvent.change(input, { target: { value: 'aut' } })

    const variables = await link.waitForRequest('CommandSearch')

    expect(variables).toEqual({ workspaceSlug: WORKSPACE_SLUG, query: 'aut' })
    expect(link.countOf('CommandSearch')).toBe(1)
  })

  it('never asks for an empty query', async () => {
    const { user, link } = await renderShell()

    pressChord()
    await user.type(paletteInput(), 'a')
    await link.resolve('CommandSearch', { data: searchData() })

    await user.clear(paletteInput())
    await link.idle()

    // The server short-circuits a blank query without touching the database,
    // so the round trip would only establish what both ends already know.
    expect(link.countOf('CommandSearch')).toBe(1)
  })

  it('groups what it found, and runs it', async () => {
    const { user, link, currentPath } = await renderShell()

    pressChord()
    fireEvent.change(paletteInput(), { target: { value: 'auth' } })

    await link.resolve('CommandSearch', {
      data: searchData(
        [{ id: issueId(42), identifier: 'ENG-42', title: 'Broken auth redirect' }],
        [{ id: issueId(7), name: 'Authentication' }],
      ),
    })

    const groups = screen.getAllByRole('group').map((group) => group.textContent ?? '')

    // Issues first, then projects, then the commands: someone who types
    // "auth" wants the issue about authentication, not the Settings screen.
    expect(groups[0]).toContain('Broken auth redirect')
    expect(groups[1]).toContain('Authentication')

    // The identifier rides along as the row's hint, which is what makes a
    // result scannable when several issues share a title.
    expect(activeOptionName()).toContain('ENG-42')

    await user.keyboard('{Enter}')

    expect(currentPath()).toBe(`/${WORKSPACE_SLUG}/issues/${issueId(42)}`)
  })

  it('says it is searching rather than saying there is nothing', async () => {
    const { link } = await renderShell()

    pressChord()
    fireEvent.change(paletteInput(), { target: { value: 'zzzz' } })

    await link.waitForRequest('CommandSearch')

    /*
      The failure this guards against is the palette answering "no matches"
      while the request is still out. A user reads that as the answer and
      stops looking.
    */
    expect(screen.queryByText('No matches')).toBeNull()

    // Scoped to the dialog: the screen behind it has a loading skeleton with
    // a status region of its own.
    const dialog = palette()

    expect(dialog).not.toBeNull()
    expect(within(dialog as HTMLElement).getByRole('status')).toHaveTextContent(
      'Searching',
    )

    /*
      Twice: once in the live region, once drawn in the footer. They are one
      string computed in one place, and this is what says so -- a footer that
      counted the results itself would read "0 results" here, which is the
      same claim the list is refusing to make one line above.
    */
    expect(within(dialog as HTMLElement).getAllByText('Searching')).toHaveLength(2)

    await link.resolve('CommandSearch', { data: searchData() })

    expect(screen.getByText('No matches')).toBeInTheDocument()
  })

  it('reports a failed search without leaking what failed', async () => {
    const { link } = await renderShell()

    pressChord()
    fireEvent.change(paletteInput(), { target: { value: 'auth' } })

    await link.fail('CommandSearch', new Error('connect ECONNREFUSED 127.0.0.1:8000'))

    const alert = screen.getByRole('alert')

    expect(alert).toHaveTextContent('Could not search this workspace')
    expect(alert).not.toHaveTextContent('ECONNREFUSED')
  })
})

describe('the create-issue action', () => {
  it('opens the composer the mounted screen registered', async () => {
    const { user, link } = await renderShell()

    await link.resolve('IssueList', { data: issueListData([issueRow(1)]) })

    pressChord()
    await user.click(screen.getByRole('option', { name: 'New issue' }))

    /*
      The composer the issue list owns, reached through the shell's
      create-issue slot -- the same one the rail's button uses. A second
      create path in the palette would be a second composer to keep in step
      with this one.
    */
    expect(screen.getByRole('heading', { name: 'New issue' })).toBeInTheDocument()
    expect(palette()).toBeNull()
  })

  it('is absent on a screen that cannot create issues', async () => {
    const view = renderApp({ initialPath: `/${WORKSPACE_SLUG}/settings` })

    await view.link.idle()

    pressChord()

    // Nothing has filled the slot, so there is no command. Not a disabled
    // one, and not one that opens a composer with nowhere to file.
    expect(screen.queryByRole('option', { name: 'New issue' })).toBeNull()
    expect(screen.getByRole('option', { name: 'Settings' })).toBeInTheDocument()
  })
})

describe('the product-wide single-key shortcuts', () => {
  /**
   * The half of a single-key shortcut that is worth testing.
   *
   * `/` opening a palette is easy and nearly worthless to assert on its own.
   * What is silent when it breaks is everything the key must *not* do: fire
   * while somebody is typing a path into a description, or steal Ctrl+/ from
   * the platform.
   */
  it.each(['input', 'textarea'] as const)(
    'does not fire inside <%s>',
    async (tagName) => {
      await renderShell()

      const field = focusOutsideTheApp(document.createElement(tagName))

      fireEvent.keyDown(field, { key: '/' })
      fireEvent.keyDown(field, { key: '?', shiftKey: true })

      expect(palette()).toBeNull()
    },
  )

  it('does not fire inside a contenteditable element', async () => {
    await renderShell()

    const editor = document.createElement('div')

    // jsdom leaves `isContentEditable` undefined however the attribute is
    // set, so the property stands in for the browser behaviour. The code
    // under test runs unmodified.
    Object.defineProperty(editor, 'isContentEditable', { value: true })
    focusOutsideTheApp(editor)

    fireEvent.keyDown(editor, { key: '/' })

    expect(palette()).toBeNull()
  })

  it('leaves the modified forms to the platform', async () => {
    await renderShell()

    fireEvent.keyDown(window, { key: '/', ctrlKey: true })
    fireEvent.keyDown(window, { key: '/', metaKey: true })

    expect(palette()).toBeNull()
  })

  it('opens the palette on / with the query field focused', async () => {
    await renderShell()

    fireEvent.keyDown(window, { key: '/' })

    expect(palette()).toBeInTheDocument()
    expect(paletteInput()).toHaveFocus()
  })

  it('opens the composer on C, through the shell slot', async () => {
    const { link } = await renderShell()

    await link.resolve('IssueList', { data: issueListData([issueRow(1)]) })

    fireEvent.keyDown(window, { key: 'c' })

    expect(screen.getByRole('heading', { name: 'New issue' })).toBeInTheDocument()
  })

  it('does nothing on C where no screen offers a composer', async () => {
    const view = renderApp({ initialPath: `/${WORKSPACE_SLUG}/settings` })

    await view.link.idle()

    fireEvent.keyDown(window, { key: 'c' })

    expect(screen.queryByRole('heading', { name: 'New issue' })).toBeNull()
  })
})

describe('the keyboard reference', () => {
  it('opens on ? and lists only shortcuts that work', async () => {
    const view = renderApp({ initialPath: `/${WORKSPACE_SLUG}/settings` })

    await view.link.idle()

    fireEvent.keyDown(window, { key: '?', shiftKey: true })

    const dialog = screen.getByRole('dialog', { name: 'Keyboard shortcuts' })

    expect(within(dialog).getByText('Open the command palette')).toBeInTheDocument()
    expect(within(dialog).getByText('Search this workspace')).toBeInTheDocument()

    // No screen here offers a composer, so `C` is not advertised. A reference
    // that documents a key nobody bound is worse than no reference.
    expect(within(dialog).queryByText('New issue')).toBeNull()
  })

  it('is reachable from the palette itself, and does not close it', async () => {
    const { user, link } = await renderShell()

    await link.resolve('IssueList', { data: issueListData([issueRow(1)]) })

    pressChord()
    await user.click(screen.getByRole('option', { name: 'Keyboard shortcuts' }))

    // The dialog's accessible name follows the view, so a screen reader is
    // told where the user now is.
    const dialog = screen.getByRole('dialog', { name: 'Keyboard shortcuts' })

    // This screen does register a composer, so `C` is advertised here.
    // Scoped, because the rail's own button carries the same words.
    expect(within(dialog).getByText('New issue')).toBeInTheDocument()

    await user.keyboard('{Escape}')

    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('goes back to the command view the next time it opens', async () => {
    const { user } = await renderShell()

    pressChord()
    await user.click(screen.getByRole('option', { name: 'Keyboard shortcuts' }))
    await user.keyboard('{Escape}')

    pressChord()

    expect(screen.getByRole('dialog', { name: 'Command palette' })).toBeInTheDocument()
  })
})
