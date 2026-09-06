import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import {
  cursor,
  issueListData,
  issueRow,
  WORKSPACE_SLUG,
} from '../../test/factories'
import { issueRowTexts, issueRows, main, renderApp } from '../../test/render'

/**
 * Cursor pagination, through the real cache.
 *
 * Every test in this file runs against `createCache()` -- the application's
 * own factory, carrying the `issues` field policy from
 * `src/lib/graphql/cache.ts`. That is not incidental. Nothing in the issues
 * feature concatenates pages: `useIssueList` has no array state and no
 * spread, and the screen renders whatever `data.issues.nodes` holds. The
 * merge, the dedupe and the reset are entirely the field policy's, so a test
 * that stubbed the cache would be asserting against a merge that does not
 * exist in the product, and a test that asserted on component state would be
 * asserting against a variable that is not there.
 *
 * The complement to these is `src/lib/graphql/cache.test.ts`, which drives
 * the same policy directly for the cases a UI cannot reach.
 */

const FIRST_CURSOR = cursor('page-one-frontier')
const SECOND_CURSOR = cursor('page-two-frontier')

/** Page one: two rows, more to come. */
async function loadFirstPage(link: ReturnType<typeof renderApp>['link']): Promise<void> {
  await link.resolve('IssueList', {
    data: issueListData(
      [issueRow(1, { title: 'Alpha' }), issueRow(2, { title: 'Bravo' })],
      { hasNextPage: true, endCursor: FIRST_CURSOR },
    ),
  })
}

function loadMoreButton(): HTMLElement {
  return within(main()).getByRole('button', { name: 'Load more' })
}

describe('issue list pagination', () => {
  it('asks for the first page with no cursor', async () => {
    const { link } = renderApp()

    const variables = await link.waitForRequest('IssueList')

    // `after: null` and not an omitted key: the merge policy reads
    // `args.after` to decide whether a result starts the list or extends it.
    // The workspace rides along on every page, because the field requires
    // one and the cache keys the list on it.
    expect(variables).toEqual({ workspaceSlug: WORKSPACE_SLUG, after: null })
  })

  it('sends the previous page endCursor as `after`, and nothing that could be an offset', async () => {
    const { link, user } = renderApp()

    await loadFirstPage(link)
    await user.click(loadMoreButton())

    const variables = await link.waitForRequest('IssueList')

    // The exact token the server handed back as the frontier of page one.
    expect(variables['after']).toBe(FIRST_CURSOR)

    /*
      And it is a cursor rather than a position. Three independent checks,
      because "we sent a cursor" is the claim most easily satisfied by
      accident:

        - the value is the opaque token, not a row count;
        - it does not parse as a number, so it cannot be an offset in
          disguise;
        - no `offset`, `skip`, `page` or `first` variable exists at all --
          `first` is a literal in the document precisely so it is never a
          variable, and the other three are concepts this feature does not
          have.
    */
    expect(Number.isNaN(Number(variables['after']))).toBe(true)
    expect(Object.keys(variables)).toEqual(['workspaceSlug', 'after'])

    // The whole sequence, in order: no cursor, then the frontier the server
    // reported. Two numbers here would mean an offset implementation.
    expect(
      link.operationsNamed('IssueList').map((operation) => operation.variables['after']),
    ).toEqual([null, FIRST_CURSOR])
  })

  it('keeps the loaded rows on screen while the next page is in flight', async () => {
    const { link, user } = renderApp()

    await loadFirstPage(link)
    expect(issueRowTexts()).toHaveLength(2)

    await user.click(loadMoreButton())
    await link.waitForRequest('IssueList')

    // Mid-flight: the rows the user was reading are still there.
    expect(issueRowTexts()).toHaveLength(2)
    expect(screen.getByText('Alpha')).toBeInTheDocument()
    expect(screen.getByText('Bravo')).toBeInTheDocument()
  })

  it('appends the next page to the rows already loaded', async () => {
    const { link, user } = renderApp()

    await loadFirstPage(link)
    await user.click(loadMoreButton())

    await link.resolve('IssueList', {
      data: issueListData(
        [issueRow(3, { title: 'Charlie' }), issueRow(4, { title: 'Delta' })],
        { hasNextPage: false, endCursor: SECOND_CURSOR },
      ),
    })

    const rows = issueRowTexts()
    expect(rows).toHaveLength(4)
    expect(rows[0]).toContain('Alpha')
    expect(rows[1]).toContain('Bravo')
    expect(rows[2]).toContain('Charlie')
    expect(rows[3]).toContain('Delta')
  })

  it('distinguishes loading more from loading the list', async () => {
    const { link, user } = renderApp()

    // The first load: a skeleton, and no rows at all.
    await link.waitForRequest('IssueList')
    const firstLoad = main().textContent ?? ''
    expect(screen.getByRole('status')).toHaveTextContent('Loading issues')
    expect(within(main()).queryByRole('list')).toBeNull()

    await loadFirstPage(link)
    await user.click(loadMoreButton())
    await link.waitForRequest('IssueList')

    // Loading more: an inline line beneath a list that is entirely on screen.
    const loadingMore = screen.getByRole('status')
    expect(loadingMore).toHaveTextContent('Loading more issues')
    expect(issueRows()).toHaveLength(2)

    // The first-load skeleton is not what is being shown.
    expect(screen.queryByText('Loading issues')).toBeNull()
    expect(main().textContent).not.toBe(firstLoad)
  })

  it('does not send a second request when the same cursor is dispatched twice', async () => {
    const { link, user } = renderApp()

    await loadFirstPage(link)
    expect(link.countOf('IssueList')).toBe(1)

    const button = loadMoreButton()

    // A double click, or a double-invoked effect: the same cursor twice
    // before the first answer arrives.
    await user.click(button)
    await user.click(button)

    expect(link.countOf('IssueList')).toBe(2)
    expect(link.inFlightCount('IssueList')).toBe(1)

    await link.resolve('IssueList', {
      data: issueListData([issueRow(3, { title: 'Charlie' })], {
        hasNextPage: false,
        endCursor: SECOND_CURSOR,
      }),
    })

    const rows = issueRowTexts()
    expect(rows).toHaveLength(3)
    expect(new Set(rows).size).toBe(3)
  })

  it("does not duplicate rows under StrictMode's double-invoked effects", async () => {
    // The application really does mount inside `<StrictMode>` (src/main.tsx),
    // and a double-invoked effect that re-ran the first page is one of the
    // ways the merge policy's reset branch earns its place. Everything below
    // must hold there too.
    const { link, user } = renderApp({ strictMode: true })

    await loadFirstPage(link)

    expect(link.countOf('IssueList')).toBe(1)
    expect(issueRowTexts()).toHaveLength(2)

    await user.click(loadMoreButton())
    await link.resolve('IssueList', {
      data: issueListData([issueRow(3, { title: 'Charlie' })], {
        hasNextPage: false,
        endCursor: SECOND_CURSOR,
      }),
    })

    const rows = issueRowTexts()
    expect(rows).toHaveLength(3)
    expect(new Set(rows).size).toBe(3)
    expect(link.countOf('IssueList')).toBe(2)
  })

  it('does not duplicate a row the next page returns again', async () => {
    const { link, user } = renderApp()

    await loadFirstPage(link)
    await user.click(loadMoreButton())

    /*
      The server repeating a row across a page boundary.

      Keyset pagination should not do this, so the guarantee under test is the
      cache policy's dedupe rather than the server's behaviour: even handed an
      overlapping page, the list must not show `Bravo` twice. This is the
      failure mode the whole design is arranged against -- it is invisible
      until there is a second page, and it looks like a rendering bug rather
      than a caching one.
    */
    await link.resolve('IssueList', {
      data: issueListData(
        [issueRow(2, { title: 'Bravo' }), issueRow(3, { title: 'Charlie' })],
        { hasNextPage: false, endCursor: SECOND_CURSOR },
      ),
    })

    const rows = issueRowTexts()

    expect(rows).toHaveLength(3)
    expect(new Set(rows).size).toBe(3)
    expect(screen.getAllByText('Bravo')).toHaveLength(1)
    expect(rows[0]).toContain('Alpha')
    expect(rows[1]).toContain('Bravo')
    expect(rows[2]).toContain('Charlie')
  })

  it('states the end of the list instead of leaving a dead button', async () => {
    const { link, user } = renderApp()

    await loadFirstPage(link)
    await user.click(loadMoreButton())

    await link.resolve('IssueList', {
      data: issueListData([issueRow(3, { title: 'Charlie' })], {
        hasNextPage: false,
        endCursor: SECOND_CURSOR,
      }),
    })

    expect(within(main()).queryByRole('button', { name: 'Load more' })).toBeNull()
    expect(screen.getByText(/End of list/)).toHaveTextContent('3 issues loaded')
  })

  it('offers no "Load more" when the first page is already the last', async () => {
    const { link } = renderApp()

    await link.resolve('IssueList', {
      data: issueListData([issueRow(1, { title: 'Alpha' })], {
        hasNextPage: false,
        endCursor: FIRST_CURSOR,
      }),
    })

    expect(within(main()).queryByRole('button', { name: 'Load more' })).toBeNull()
    expect(screen.getByText(/End of list/)).toHaveTextContent('1 issue loaded')
  })

  it('keeps the loaded rows when loading more fails, and can retry', async () => {
    const { link, user } = renderApp()

    await loadFirstPage(link)
    await user.click(loadMoreButton())

    await link.fail('IssueList', new Error('Failed to fetch'))

    // Rows survive the failure: throwing away good data to display an error
    // is not error handling.
    expect(issueRows()).toHaveLength(2)

    const alert = within(main()).getByRole('alert')
    expect(alert).toHaveTextContent('Failed to fetch')

    await user.click(within(alert).getByRole('button', { name: 'Try again' }))

    const retryVariables = await link.waitForRequest('IssueList')
    // The retry resumes from the same frontier -- it does not restart the
    // list, which would discard the pages already loaded.
    expect(retryVariables['after']).toBe(FIRST_CURSOR)

    await link.resolve('IssueList', {
      data: issueListData([issueRow(3, { title: 'Charlie' })], {
        hasNextPage: false,
        endCursor: SECOND_CURSOR,
      }),
    })

    expect(issueRowTexts()).toHaveLength(3)
    expect(within(main()).queryByRole('alert')).toBeNull()
  })
})
