import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { Button } from '../Button'
import { Dialog } from './Dialog'

/**
 * What is tested here is the wiring, not the platform.
 *
 * The focus trap, the initial focus and the return of focus on close are the
 * browser's implementation of `showModal()`; they are not ours to break and
 * jsdom does not implement them anyway. What *is* ours -- and what silently
 * fails without a test -- is the two-way sync: telling the element to open when
 * the prop says so, and telling the caller when the element closed itself.
 */

function Harness({ onClose }: { onClose?: () => void }) {
  const [open, setOpen] = useState(false)

  return (
    <>
      <Button
        onClick={() => {
          setOpen(true)
        }}
      >
        Open
      </Button>
      <Dialog
        open={open}
        onClose={() => {
          setOpen(false)
          onClose?.()
        }}
        title="Delete issue"
        description="This cannot be undone."
      >
        <p>Body</p>
      </Dialog>
    </>
  )
}

function dialogElement(): HTMLDialogElement {
  const element = document.querySelector('dialog')
  if (element === null) throw new Error('no <dialog> rendered')
  return element
}

describe('Dialog', () => {
  it('renders nothing inside until it is opened', () => {
    render(<Harness />)

    expect(dialogElement().open).toBe(false)
    expect(screen.queryByText('Body')).not.toBeInTheDocument()
  })

  it('opens, and takes its accessible name and description from the props', async () => {
    const user = userEvent.setup()
    render(<Harness />)

    await user.click(screen.getByRole('button', { name: 'Open' }))

    const dialog = screen.getByRole('dialog')
    expect(dialog).toHaveAccessibleName('Delete issue')
    expect(dialog).toHaveAccessibleDescription('This cannot be undone.')
    expect(screen.getByText('Body')).toBeInTheDocument()
  })

  it('closes from its own close button', async () => {
    const user = userEvent.setup()
    const onClose = vi.fn()
    render(<Harness onClose={onClose} />)

    await user.click(screen.getByRole('button', { name: 'Open' }))
    await user.click(screen.getByRole('button', { name: 'Close dialog' }))

    expect(onClose).toHaveBeenCalledOnce()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  /**
   * Escape is handled by the browser, which closes the element and fires
   * `close` without asking us. The bug this guards against is the component not
   * listening: `open` would stay `true` while the element was shut, and the next
   * click on the trigger would set a prop that had not changed -- leaving a
   * dialog that can be opened exactly once. Dispatching `close` is how that path
   * is exercised without a browser.
   */
  it('tells the caller when the element closes itself', () => {
    const onClose = vi.fn()
    render(<Harness onClose={onClose} />)

    act(() => {
      dialogElement().dispatchEvent(new Event('close'))
    })

    expect(onClose).toHaveBeenCalledOnce()
  })

  it('closes when the scrim is clicked, and not when the panel is', async () => {
    const user = userEvent.setup()
    const onClose = vi.fn()
    render(<Harness onClose={onClose} />)

    await user.click(screen.getByRole('button', { name: 'Open' }))

    await user.click(screen.getByText('Body'))
    expect(onClose).not.toHaveBeenCalled()

    // A click on the backdrop is retargeted to the <dialog> element itself.
    await user.click(dialogElement())
    expect(onClose).toHaveBeenCalledOnce()
  })
})
