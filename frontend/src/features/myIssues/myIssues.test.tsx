import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { main, renderApp } from '../../test/render'
import {
  MEMBER_ID,
  OTHER_MEMBER_ID,
  WORKSPACE_SLUG,
  cursor,
  issueListData,
  issueRow,
  workspaceContextData,
} from '../../test/factories'

/**
 * My Issues, against the real router, the real cache and a controlled network.
 *
 * The screen's whole risk is the client-side filter: the API has no
 * `assigneeId` argument, so "mine" is decided in the browser over whatever
 * page is loaded. What can break is not the filtering -- it is the screen
 * quietly presenting a partial answer as a complete one, which is invisible
 * unless a test looks for the sentence that says otherwise.
 *
 * `VIEWER_ID` and `MEMBER_ID` are the same id in the factories, so an issue
 * assigned to `MEMBER_ID` is assigned to the viewer the shell publishes.
 */

const PATH = `/${WORKSPACE_SLUG}/my-issues`

/** One page: two issues assigned to the viewer, one to somebody else. */
function mixedPage(hasNextPage = false) {
  return issueListData(
    [
      issueRow(1, { assigneeId: MEMBER_ID, title: 'Mine one' }),
      issueRow(2, { assigneeId: OTHER_MEMBER_ID, title: 'Not mine' }),
      issueRow(3, { assigneeId: MEMBER_ID, title: 'Mine two' }),
    ],
    { hasNextPage, endCursor: hasNextPage ? cursor('page-1') : null },
  )
}

describe('my issues', () => {
  it('asks for the workspace in the URL', async () => {
    const view = renderApp({ initialPath: PATH })

    // The one thing a tenant-scoped screen must get right: the slug comes
    // from the address bar and nowhere else.
    await expect(view.link.waitForRequest('IssueList')).resolves.toMatchObject({
      workspaceSlug: WORKSPACE_SLUG,
    })
  })

  it('shows only the viewer’s issues, and says what it filtered', async () => {
    const view = renderApp({ initialPath: PATH })

    await view.link.resolve('IssueList', { data: mixedPage() })
    await view.link.resolve('IssueWorkspaceContext', { data: workspaceContextData() })

    const list = within(main()).getByRole('list', { name: 'Issues assigned to you' })

    expect(within(list).getByText('Mine one')).toBeInTheDocument()
    expect(within(list).getByText('Mine two')).toBeInTheDocument()
    expect(within(list).queryByText('Not mine')).not.toBeInTheDocument()

    // The honesty this screen exists to keep: the denominator is what was
    // loaded, not what the workspace holds.
    expect(
      within(main()).getByText('2 of 3 loaded issues assigned to you.'),
    ).toBeInTheDocument()
  })

  it('says an empty result may only be empty so far', async () => {
    const view = renderApp({ initialPath: PATH })

    await view.link.resolve('IssueList', {
      data: issueListData([issueRow(2, { assigneeId: OTHER_MEMBER_ID })], {
        hasNextPage: true,
        endCursor: cursor('page-1'),
      }),
    })
    await view.link.resolve('IssueWorkspaceContext', { data: workspaceContextData() })

    // "None loaded" and "none at all" are different claims, and only one of
    // them is true while there is another page to fetch.
    expect(within(main()).getByText('Nothing assigned to you')).toBeInTheDocument()
    expect(
      within(main()).getByText(/Older issues may be assigned to you/),
    ).toBeInTheDocument()
  })

  it('claims to have checked everything only when the list has ended', async () => {
    const view = renderApp({ initialPath: PATH })

    await view.link.resolve('IssueList', {
      data: issueListData([issueRow(2, { assigneeId: OTHER_MEMBER_ID })]),
    })
    await view.link.resolve('IssueWorkspaceContext', { data: workspaceContextData() })

    expect(
      within(main()).getByText(/Every issue in this workspace has been checked/),
    ).toBeInTheDocument()
  })

  it('says the load-more button loads the workspace, not your issues', async () => {
    const view = renderApp({ initialPath: PATH })

    await view.link.resolve('IssueList', { data: mixedPage(true) })
    await view.link.resolve('IssueWorkspaceContext', { data: workspaceContextData() })

    // A button labelled "Load more" beside a filtered list is a promise the
    // screen cannot keep.
    expect(
      screen.getByRole('button', { name: 'Load more workspace issues' }),
    ).toBeInTheDocument()
  })
})
