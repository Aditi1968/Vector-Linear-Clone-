import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { List } from '../List'
import { IssueRow } from './IssueRow'

function renderRow(props: Partial<Parameters<typeof IssueRow>[0]> = {}) {
  return render(
    <List label="Issues">
      <IssueRow
        identifier="ENG-142"
        title={<a href="/acme/issues/1">Keyset cursor drops rows</a>}
        {...props}
      />
    </List>,
  )
}

/**
 * The shared issue row.
 *
 * It is presentational, so most of it is not worth a test. Three things are,
 * because every screen built on this row inherits them and each fails
 * silently: the row is one tab stop and that stop is a real link, a glyph
 * that was not supplied leaves a gap rather than a guess, and the row is a
 * list item so the list around it can announce its own length.
 */
describe('IssueRow', () => {
  /*
    The row is a `<li>` and not a bare div. `List` states `role="list"`
    explicitly to survive `list-style: none` in Safari, and that only means
    something if what is inside it are list items.
  */
  it('is a list item, so the list can count itself', () => {
    renderRow()

    expect(within(screen.getByRole('list')).getAllByRole('listitem')).toHaveLength(1)
  })

  /*
    One tab stop, and it is a link.

    A row built as `<div onClick>` would pass any "clicking works" assertion
    and fail every keyboard user; a row that made the project chip a second
    link would double the tab stops and stop "the number of links" meaning
    "the number of issues". Both regressions show up here as a count.
  */
  it('exposes exactly one link, named by the title', () => {
    renderRow({
      labels: <span>pagination</span>,
      meta: <span>2h</span>,
    })

    const links = screen.getAllByRole('link')

    expect(links).toHaveLength(1)
    expect(links[0]).toHaveAccessibleName('Keyset cursor drops rows')
  })

  /*
    An unresolved workflow state renders nothing, not a fallback.

    A plausible-looking backlog ring for a state we could not look up is a
    lie about the issue, and it is the kind that survives review because it
    looks right. The assertion is on the *absence* of an announced glyph:
    the indicators render `role="img"` with an accessible name.
  */
  it('leaves the glyph columns empty rather than guessing', () => {
    renderRow()

    expect(screen.queryAllByRole('img')).toHaveLength(0)
  })

  it('announces both glyphs when they are supplied', () => {
    renderRow({ priority: 'urgent', status: 'started', statusName: 'In progress' })

    expect(screen.getByRole('img', { name: 'Priority: Urgent' })).toBeInTheDocument()
    expect(
      screen.getByRole('img', { name: 'Status: In progress' }),
    ).toBeInTheDocument()
  })

  it('renders the identifier', () => {
    renderRow()

    expect(screen.getByText('ENG-142')).toBeInTheDocument()
  })
})
