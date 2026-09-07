import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { main, renderApp } from '../../test/render'
import {
  MEMBER_ID,
  WORKSPACE_SLUG,
  cursor,
  issueListData,
  issueRow,
  workspaceContextData,
} from '../../test/factories'

/**
 * My Issues, against the real router, the real cache and a controlled network.
 *
 * The screen's whole risk is now the filter it sends. "Assigned to me" is one
 * key of `IssueFilterInput`, and the input spells "unassigned" as an explicit
 * `assigneeId: null` -- so a screen that routed an optional viewer id through
 * the filter would render somebody else's unassigned work under a heading
 * claiming it is yours, with nothing on screen to suggest it. That is what
 * the first two tests below are about; the rest are about the counts, which
 * are the server's now and no longer "among those loaded".
 *
 * `VIEWER_ID` and `MEMBER_ID` are the same id in the factories, so an issue
 * assigned to `MEMBER_ID` is assigned to the viewer the shell publishes.
 */

const PATH = `/${WORKSPACE_SLUG}/my-issues`

/** One page of the viewer's issues, as the server would answer the filter. */
function minePage(options: { hasNextPage?: boolean; totalCount?: number } = {}) {
  return issueListData(
    [
      issueRow(1, { assigneeId: MEMBER_ID, title: 'Mine one' }),
      issueRow(3, { assigneeId: MEMBER_ID, title: 'Mine two' }),
    ],
    {
      ...options,
      endCursor: options.hasNextPage === true ? cursor('page-1') : null,
    },
  )
}

describe('my issues', () => {
  it('asks the server for the viewer’s issues, in the workspace in the URL', async () => {
    const view = renderApp({ initialPath: PATH })

    // `toEqual` and not `toMatchObject`: the claim is the shape of the whole
    // filter. An extra key here would be an extra predicate on the server.
    await expect(view.link.waitForRequest('IssueList')).resolves.toEqual({
      workspaceSlug: WORKSPACE_SLUG,
      filter: { assigneeId: MEMBER_ID },
      after: null,
    })
  })

  it('sends the assignee as an id and never as a null', async () => {
    const view = renderApp({ initialPath: PATH })

    const variables = await view.link.waitForRequest('IssueList')
    const filter = variables['filter']

    // The failure this guards is not a crash: `assigneeId: null` is a valid
    // filter meaning "unassigned", so the wrong request returns a plausible
    // list of the wrong issues.
    expect(filter).not.toBeNull()
    expect((filter as Record<string, unknown>)['assigneeId']).toBe(MEMBER_ID)
  })

  it('shows the rows the server sent, and states the real total', async () => {
    const view = renderApp({ initialPath: PATH })

    await view.link.resolve('IssueList', { data: minePage() })
    await view.link.resolve('IssueWorkspaceContext', { data: workspaceContextData() })

    const list = within(main()).getByRole('list', { name: 'Issues assigned to you' })

    expect(within(list).getByText('Mine one')).toBeInTheDocument()
    expect(within(list).getByText('Mine two')).toBeInTheDocument()

    // `totalCount`, not the length of the page: the whole answer is loaded,
    // so the sentence is about the workspace rather than about the screen.
    expect(within(main()).getByText('2 issues assigned to you.')).toBeInTheDocument()
  })

  it('says how much of the total is on screen while there is more to load', async () => {
    const view = renderApp({ initialPath: PATH })

    await view.link.resolve('IssueList', {
      data: minePage({ hasNextPage: true, totalCount: 9 }),
    })
    await view.link.resolve('IssueWorkspaceContext', { data: workspaceContextData() })

    // Still paginated, so the honest sentence survives -- reworded around a
    // number that is now true rather than around what happened to be loaded.
    expect(
      within(main()).getByText('Showing 2 of 9 issues assigned to you.'),
    ).toBeInTheDocument()

    // And the button loads more of *your* issues now, so it says so by
    // saying nothing special.
    expect(screen.getByRole('button', { name: 'Load more' })).toBeInTheDocument()
  })

  it('says nothing is assigned to you, without hedging about pages', async () => {
    const view = renderApp({ initialPath: PATH })

    await view.link.resolve('IssueList', { data: issueListData([]) })
    await view.link.resolve('IssueWorkspaceContext', { data: workspaceContextData() })

    expect(within(main()).getByText('Nothing assigned to you')).toBeInTheDocument()
    expect(
      within(main()).getByText('No issue in this workspace is assigned to you.'),
    ).toBeInTheDocument()
  })
})
