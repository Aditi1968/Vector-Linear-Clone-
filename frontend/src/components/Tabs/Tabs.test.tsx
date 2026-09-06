import { useState } from 'react'
import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { Tabs } from './Tabs'

function Harness() {
  const [value, setValue] = useState('all')

  return (
    <Tabs
      label="Issue views"
      value={value}
      onChange={setValue}
      tabs={[
        { id: 'all', label: 'All', content: <p>All issues</p> },
        { id: 'active', label: 'Active', count: 4, content: <p>Active issues</p> },
        { id: 'done', label: 'Done', content: <p>Done issues</p> },
      ]}
    />
  )
}

describe('Tabs', () => {
  it('exposes one tab stop, not one per tab', () => {
    render(<Harness />)

    // Roving tabindex: a tablist of eight tabs must not cost eight Tab presses
    // to walk past.
    expect(screen.getByRole('tab', { name: 'All' })).toHaveAttribute('tabindex', '0')
    expect(screen.getByRole('tab', { name: /Active/ })).toHaveAttribute(
      'tabindex',
      '-1',
    )
  })

  it('shows only the selected panel, labelled by its tab', () => {
    render(<Harness />)

    const panel = screen.getByRole('tabpanel')
    expect(panel).toHaveAccessibleName('All')
    expect(screen.queryByText('Done issues')).not.toBeInTheDocument()
  })

  it('moves and selects with the arrow keys, wrapping at both ends', async () => {
    const user = userEvent.setup()
    render(<Harness />)

    screen.getByRole('tab', { name: 'All' }).focus()

    await user.keyboard('{ArrowRight}')
    expect(screen.getByRole('tab', { name: /Active/ })).toHaveFocus()
    expect(screen.getByRole('tabpanel')).toHaveTextContent('Active issues')

    await user.keyboard('{ArrowRight}{ArrowRight}')
    expect(screen.getByRole('tab', { name: 'All' })).toHaveFocus()

    await user.keyboard('{ArrowLeft}')
    expect(screen.getByRole('tab', { name: 'Done' })).toHaveFocus()
  })

  it('jumps to the ends with Home and End', async () => {
    const user = userEvent.setup()
    render(<Harness />)

    screen.getByRole('tab', { name: 'All' }).focus()

    await user.keyboard('{End}')
    expect(screen.getByRole('tab', { name: 'Done' })).toHaveAttribute(
      'aria-selected',
      'true',
    )

    await user.keyboard('{Home}')
    expect(screen.getByRole('tab', { name: 'All' })).toHaveAttribute(
      'aria-selected',
      'true',
    )
  })
})
