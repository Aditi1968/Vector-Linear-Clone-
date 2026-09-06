import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { IconButton } from '../Button'
import { SearchIcon } from '../icons'
import { Tooltip } from './Tooltip'

function renderTooltip() {
  const user = userEvent.setup()
  render(
    <Tooltip label="Search issues">
      <IconButton icon={<SearchIcon />} aria-label="Search" />
    </Tooltip>,
  )
  return { user, trigger: screen.getByRole('button', { name: 'Search' }) }
}

describe('Tooltip', () => {
  it('is absent from the accessibility tree until it opens', () => {
    renderTooltip()

    // A permanently rendered, visually hidden copy would be read out on every
    // pass through the page.
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
  })

  /**
   * The half people forget. An icon-only toolbar is exactly where tooltips get
   * used, and a hover-only tooltip is invisible to everyone driving the app
   * from the keyboard.
   */
  it('opens on keyboard focus, not only on hover', async () => {
    const { user } = renderTooltip()

    await user.tab()

    expect(screen.getByRole('tooltip')).toHaveTextContent('Search issues')
  })

  it('describes the trigger rather than naming it', async () => {
    const { user, trigger } = renderTooltip()

    await user.tab()

    // The button's name still comes from its own aria-label: a tooltip is
    // unreachable on touch and must never be a control's only name.
    expect(trigger).toHaveAccessibleName('Search')
    expect(trigger).toHaveAccessibleDescription('Search issues')
  })

  it('dismisses on Escape without moving the pointer', async () => {
    const { user } = renderTooltip()

    await user.tab()
    expect(screen.getByRole('tooltip')).toBeInTheDocument()

    await user.keyboard('{Escape}')
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
  })

  it('closes when focus leaves', async () => {
    const { user } = renderTooltip()

    await user.tab()
    await user.tab()

    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
  })
})
