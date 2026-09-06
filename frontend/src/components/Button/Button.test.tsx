import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { Button } from './Button'
import { IconButton } from './IconButton'
import { PlusIcon } from '../icons'

describe('Button', () => {
  it('defaults to type="button" so it cannot submit a form by accident', () => {
    render(<Button>Save</Button>)

    expect(screen.getByRole('button', { name: 'Save' })).toHaveAttribute(
      'type',
      'button',
    )
  })

  /**
   * The loading state is the one with real behaviour behind it, and the reason
   * for that behaviour is invisible in the markup: a `disabled` button drops out
   * of the tab order, so the browser moves focus to `<body>` and a keyboard user
   * who just pressed Enter on "Save" loses their place. `aria-disabled` keeps
   * focus and announces the same state, which only works if the click handler
   * is what makes the button inert.
   */
  it('stays focusable while busy, and swallows the click', async () => {
    const user = userEvent.setup()
    const onClick = vi.fn()
    render(
      <Button loading onClick={onClick}>
        Save
      </Button>,
    )

    const button = screen.getByRole('button', { name: 'Save' })
    expect(button).toHaveAttribute('aria-busy', 'true')
    expect(button).toHaveAttribute('aria-disabled', 'true')
    expect(button).not.toBeDisabled()

    button.focus()
    await user.click(button)

    expect(onClick).not.toHaveBeenCalled()
    expect(button).toHaveFocus()
  })

  it('keeps its label while loading, so the width does not jump', () => {
    render(<Button loading>Save</Button>)

    expect(screen.getByRole('button', { name: 'Save' })).toBeInTheDocument()
  })

  it('fires normally when it is not busy', async () => {
    const user = userEvent.setup()
    const onClick = vi.fn()
    render(<Button onClick={onClick}>Save</Button>)

    await user.click(screen.getByRole('button', { name: 'Save' }))

    expect(onClick).toHaveBeenCalledOnce()
  })
})

describe('IconButton', () => {
  it('is named by its aria-label, since it has no text', () => {
    render(<IconButton icon={<PlusIcon />} aria-label="Create issue" />)

    expect(screen.getByRole('button', { name: 'Create issue' })).toBeInTheDocument()
  })
})
