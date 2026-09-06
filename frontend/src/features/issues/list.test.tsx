import { cleanup, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { issueListData, issueRow } from '../../test/factories'
import { main, renderApp } from '../../test/render'

/**
 * The issue list's four outcomes.
 *
 * The thing these tests are really guarding is that the outcomes are *not
 * interchangeable*. It is easy to write a suite where each state passes
 * because something rendered, and where a regression that showed the empty
 * panel during loading -- or the error panel over a successful empty
 * response -- would go unnoticed by every assertion. So each state is
 * described by the same set of markers, and every test asserts the full set:
 * one marker present and the other three absent.
 */

interface ListStateMarkers {
  /** The first-load skeleton's hidden status text. */
  loading: boolean
  /** A rendered list of rows. */
  rows: number
  /** The "there are none" panel, which is a success. */
  empty: boolean
  /** The "asking failed" panel, which is not. */
  error: boolean
}

function markers(): ListStateMarkers {
  const list = within(main()).queryByRole('list')

  return {
    loading: screen.queryByText('Loading issues') !== null,
    rows: list === null ? 0 : within(list).getAllByRole('link').length,
    empty: screen.queryByText('No issues yet') !== null,
    error: screen.queryByText('Could not load issues') !== null,
  }
}

describe('issue list', () => {
  it('shows a loading state before the first response arrives', async () => {
    const { link } = renderApp()

    // The request is in flight and unanswered, which is exactly the window
    // the skeleton exists for.
    await link.waitForRequest('IssueList')

    expect(markers()).toEqual({ loading: true, rows: 0, empty: false, error: false })

    // Announced, not merely drawn. A purely visual loading state is silence
    // to a screen reader.
    const status = screen.getByRole('status')
    expect(status).toHaveAttribute('aria-busy', 'true')
    expect(status).toHaveTextContent('Loading issues')
  })

  it('shows rows once the response arrives', async () => {
    const { link } = renderApp()

    await link.resolve('IssueList', {
      data: issueListData([
        issueRow(1, { title: 'Alpha' }),
        issueRow(2, { title: 'Bravo' }),
        issueRow(3, { title: 'Charlie' }),
      ]),
    })

    expect(markers()).toEqual({ loading: false, rows: 3, empty: false, error: false })

    const rows = within(main()).getAllByRole('listitem')
    expect(rows).toHaveLength(3)
    expect(rows[0]).toHaveTextContent('Alpha')
    expect(rows[1]).toHaveTextContent('Bravo')
    expect(rows[2]).toHaveTextContent('Charlie')
  })

  it('shows an empty state when the server answers with no issues', async () => {
    const { link } = renderApp()

    await link.resolve('IssueList', { data: issueListData([]) })

    expect(markers()).toEqual({ loading: false, rows: 0, empty: true, error: false })

    // A success, offered the one action that changes the situation.
    expect(
      screen.getByRole('button', { name: 'Create the first issue' }),
    ).toBeEnabled()
  })

  it('shows an error state when the query fails', async () => {
    const { link } = renderApp()

    // A top-level GraphQL error: the shape the backend produces for an
    // invalid `after`, and the shape a resolver failure takes.
    await link.resolve('IssueList', {
      errors: [{ message: 'Invalid pagination arguments' }],
    })

    expect(markers()).toEqual({ loading: false, rows: 0, empty: false, error: true })

    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent('Could not load issues')
    // The server's own message, not a generic apology that leaves the user
    // with nothing to act on.
    expect(alert).toHaveTextContent('Invalid pagination arguments')
  })

  it('shows an error state when the transport fails', async () => {
    const { link } = renderApp()

    await link.fail('IssueList', new Error('Failed to fetch'))

    expect(markers()).toEqual({ loading: false, rows: 0, empty: false, error: true })
    expect(screen.getByRole('alert')).toHaveTextContent('Failed to fetch')
  })

  it('retries from the error state and recovers', async () => {
    const { link, user } = renderApp()

    await link.fail('IssueList', new Error('Failed to fetch'))

    const retry = within(screen.getByRole('alert')).getByRole('button', {
      name: 'Try again',
    })

    await user.click(retry)

    // A second request really went out, rather than the button clearing the
    // panel and pretending.
    expect(link.countOf('IssueList')).toBe(2)

    await link.resolve('IssueList', {
      data: issueListData([issueRow(1, { title: 'Recovered' })]),
    })

    expect(markers()).toEqual({ loading: false, rows: 1, empty: false, error: false })
    expect(screen.getByText('Recovered')).toBeInTheDocument()
  })

  it('renders loading, rows, empty and error as four different screens', async () => {
    /*
      The cross-check the four tests above cannot make individually: that no
      two of these outcomes look the same to a user. Comparing rendered text
      catches the regressions that a per-state assertion misses -- an empty
      state that renders nothing at all and is therefore indistinguishable
      from a loading state that renders nothing at all.
    */
    const rendered = new Map<string, string>()

    const scenarios: readonly [string, (link: RenderedLink) => Promise<void>][] = [
      ['loading', async (link) => { await link.waitForRequest('IssueList') }],
      [
        'rows',
        async (link) => {
          await link.resolve('IssueList', {
            data: issueListData([issueRow(1, { title: 'Alpha' })]),
          })
        },
      ],
      [
        'empty',
        async (link) => {
          await link.resolve('IssueList', { data: issueListData([]) })
        },
      ],
      [
        'error',
        async (link) => {
          await link.fail('IssueList', new Error('Failed to fetch'))
        },
      ],
    ]

    for (const [name, drive] of scenarios) {
      const { link } = renderApp()
      await drive(link)
      rendered.set(name, main().textContent ?? '')
      cleanup()
    }

    const texts = [...rendered.values()]
    expect(new Set(texts).size).toBe(texts.length)

    // And each is non-empty: "distinct" must not be satisfied by two blank
    // screens differing in whitespace.
    for (const [name, text] of rendered) {
      expect(text.trim().length, `${name} rendered nothing`).toBeGreaterThan(0)
    }
  })
})

/** Narrow alias so the scenario table above stays readable. */
type RenderedLink = ReturnType<typeof renderApp>['link']
