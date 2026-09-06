import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { Menu } from './Menu'

/**
 * The menu's keyboard contract, which is the only part of it that can quietly
 * break. The styling cannot regress into unusability; the focus handling can,
 * and it fails invisibly for everyone driving the app from a mouse.
 */

function renderMenu(onSelect = vi.fn()) {
  const user = userEvent.setup()
  render(
    <Menu
      label="Issue actions"
      items={[
        { id: 'edit', label: 'Edit', onSelect },
        { id: 'copy', label: 'Copy link', onSelect },
        { id: 'archive', label: 'Archive', disabled: true, onSelect },
        { id: 'delete', label: 'Delete', destructive: true, onSelect },
      ]}
    />,
  )
  return { user, onSelect, trigger: screen.getByRole('button', { name: 'Issue actions' }) }
}

describe('Menu', () => {
  it('is closed until asked, and says so on the trigger', () => {
    const { trigger } = renderMenu()

    expect(trigger).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
  })

  it('opens on ArrowDown and lands on the first item', async () => {
    const { user, trigger } = renderMenu()

    trigger.focus()
    await user.keyboard('{ArrowDown}')

    expect(screen.getByRole('menu')).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: 'Edit' })).toHaveFocus()
  })

  it('opens on ArrowUp and lands on the last item', async () => {
    const { user, trigger } = renderMenu()

    trigger.focus()
    await user.keyboard('{ArrowUp}')

    expect(screen.getByRole('menuitem', { name: 'Delete' })).toHaveFocus()
  })

  it('moves with the arrow keys, wrapping past both ends and skipping disabled items', async () => {
    const { user, trigger } = renderMenu()

    await user.click(trigger)
    expect(screen.getByRole('menuitem', { name: 'Edit' })).toHaveFocus()

    await user.keyboard('{ArrowDown}')
    expect(screen.getByRole('menuitem', { name: 'Copy link' })).toHaveFocus()

    // Archive is disabled and must not receive focus.
    await user.keyboard('{ArrowDown}')
    expect(screen.getByRole('menuitem', { name: 'Delete' })).toHaveFocus()

    // Past the end, back to the top.
    await user.keyboard('{ArrowDown}')
    expect(screen.getByRole('menuitem', { name: 'Edit' })).toHaveFocus()

    // And backwards past the start.
    await user.keyboard('{ArrowUp}')
    expect(screen.getByRole('menuitem', { name: 'Delete' })).toHaveFocus()
  })

  it('jumps to the ends with Home and End', async () => {
    const { user, trigger } = renderMenu()

    await user.click(trigger)
    await user.keyboard('{End}')
    expect(screen.getByRole('menuitem', { name: 'Delete' })).toHaveFocus()

    await user.keyboard('{Home}')
    expect(screen.getByRole('menuitem', { name: 'Edit' })).toHaveFocus()
  })

  it('closes on Escape and puts focus back on the trigger', async () => {
    const { user, trigger } = renderMenu()

    await user.click(trigger)
    await user.keyboard('{Escape}')

    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
    // The whole point: without this, focus falls to <body> and a keyboard user
    // has to tab in from the top of the page to get back to where they were.
    expect(trigger).toHaveFocus()
  })

  it('runs the action, closes, and returns focus when an item is chosen', async () => {
    const onSelect = vi.fn()
    const { user, trigger } = renderMenu(onSelect)

    await user.click(trigger)
    await user.click(screen.getByRole('menuitem', { name: 'Copy link' }))

    expect(onSelect).toHaveBeenCalledOnce()
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
    expect(trigger).toHaveFocus()
  })

  it('closes when a press lands outside it', async () => {
    const { user, trigger } = renderMenu()

    await user.click(trigger)
    await user.click(document.body)

    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
  })

  it('closes on Tab without trapping focus', async () => {
    const { user, trigger } = renderMenu()

    await user.click(trigger)
    await user.tab()

    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
  })
})
