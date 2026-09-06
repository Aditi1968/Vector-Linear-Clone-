import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import {
  issueDetail,
  issueDetailData,
  issueId,
  issueListData,
  issueRow,
  workspaceContextData,
  WORKSPACE_SLUG,
} from '../../test/factories'
import { issueRows, renderApp } from '../../test/render'

/**
 * Getting from the list to one issue, and back.
 *
 * The property worth protecting is that the *URL* is the selection. Nothing
 * is handed from the list to the inspector; the id comes from the route and
 * the query runs from it, which is what makes a reload of
 * `/:workspaceSlug/issues/<id>` render the same screen by the same path. A
 * test that only clicked a row and looked for a title would pass just as well
 * against an implementation that smuggled the issue across in memory and
 * broke on refresh -- so the refresh case is asserted directly, by starting
 * the router at the deep link with nothing in the cache.
 *
 * The workspace is part of that URL for the same reason the id is: the server
 * requires one on every field, so a detail request carries the slug from the
 * route and not from anywhere a reload would lose.
 */

const ALPHA_ID = issueId(1)

const LIST_PATH = `/${WORKSPACE_SLUG}/issues`
const detailPath = (id: string) => `${LIST_PATH}/${id}`

/** The inspector, which is the screen's complementary landmark. */
function inspector(): HTMLElement {
  return screen.getByRole('complementary', { name: 'Issue detail' })
}

describe('opening an issue beside the list', () => {
  it('opens the inspector from a row and puts the id in the URL', async () => {
    const { link, user, currentPath } = renderApp()

    await link.resolve('IssueList', {
      data: issueListData([
        issueRow(1, { title: 'Alpha' }),
        issueRow(2, { title: 'Bravo' }),
      ]),
    })

    const [firstRow] = issueRows()
    expect(firstRow).toBeDefined()
    expect(firstRow).toHaveAccessibleName(/Alpha/)

    await user.click(screen.getByRole('link', { name: /Alpha/ }))

    expect(currentPath()).toBe(detailPath(ALPHA_ID))

    // The route drives the query: the id that went out is the id in the URL.
    const variables = await link.waitForRequest('IssueDetail')
    expect(variables).toEqual({ workspaceSlug: WORKSPACE_SLUG, id: ALPHA_ID })

    await link.resolve('IssueDetail', {
      data: issueDetailData(
        issueDetail(1, { title: 'Alpha', description: 'The redirect loops.' }),
      ),
    })

    // The list is still there. That is the whole point of the split pane, and
    // it is the assertion that fails if the detail view ever goes back to
    // being a screen of its own.
    expect(issueRows()).toHaveLength(2)

    const panel = inspector()
    expect(within(panel).getByRole('heading', { level: 2 })).toHaveTextContent('ENG-1')
    expect(within(panel).getByRole('textbox', { name: 'Title' })).toHaveValue('Alpha')
    expect(within(panel).getByRole('textbox', { name: 'Description' })).toHaveValue(
      'The redirect loops.',
    )

    // The page's own heading does not change: the list is still the page.
    expect(screen.getByRole('heading', { level: 1 })).toHaveAccessibleName('Issues')
  })

  it('marks the open row as the current one', async () => {
    const { link, user } = renderApp()

    await link.resolve('IssueList', {
      data: issueListData([
        issueRow(1, { title: 'Alpha' }),
        issueRow(2, { title: 'Bravo' }),
      ]),
    })

    await user.click(screen.getByRole('link', { name: /Alpha/ }))
    await link.resolve('IssueDetail', {
      data: issueDetailData(issueDetail(1, { title: 'Alpha' })),
    })

    // Announced, not merely tinted. A background colour is not information.
    const [alpha, bravo] = issueRows()
    expect(alpha).toHaveAttribute('aria-current', 'page')
    expect(bravo).not.toHaveAttribute('aria-current')
  })

  it('renders the issue when its URL is opened directly', async () => {
    // The refresh case: a cold router, an empty cache, and nothing but the
    // URL to say which issue this is.
    const { link } = renderApp({ initialPath: detailPath(ALPHA_ID) })

    const variables = await link.waitForRequest('IssueDetail')
    expect(variables).toEqual({ workspaceSlug: WORKSPACE_SLUG, id: ALPHA_ID })

    await link.resolve('IssueDetail', {
      data: issueDetailData(issueDetail(1, { title: 'Alpha' })),
    })

    expect(
      within(inspector()).getByRole('textbox', { name: 'Title' }),
    ).toHaveValue('Alpha')

    // And the list beside it is requested too, because the split pane is one
    // screen: arriving by link gives the same thing arriving by click does.
    expect(link.countOf('IssueList')).toBe(1)
  })

  it('closes back to the list without refetching it', async () => {
    const { link, user, currentPath } = renderApp()

    await link.resolve('IssueList', {
      data: issueListData([issueRow(1, { title: 'Alpha' })]),
    })

    await user.click(screen.getByRole('link', { name: /Alpha/ }))
    await link.resolve('IssueDetail', {
      data: issueDetailData(issueDetail(1, { title: 'Alpha' })),
    })

    await user.click(screen.getByRole('button', { name: 'Close issue' }))

    expect(currentPath()).toBe(LIST_PATH)
    expect(screen.queryByRole('complementary', { name: 'Issue detail' })).toBeNull()
    expect(screen.getByRole('link', { name: /Alpha/ })).toBeInTheDocument()

    /*
      Still one list request. The rows come back from the cache, and that
      matters beyond speed: a refetch sends `after: null`, which the merge
      policy reads as "start the list over" and which would drop every page
      loaded past the first.
    */
    expect(link.countOf('IssueList')).toBe(1)
  })

  it('closes on Escape', async () => {
    const { link, user, currentPath } = renderApp({
      initialPath: detailPath(ALPHA_ID),
    })

    await link.resolve('IssueDetail', {
      data: issueDetailData(issueDetail(1, { title: 'Alpha' })),
    })

    await user.keyboard('{Escape}')

    expect(currentPath()).toBe(LIST_PATH)
  })

  it('treats a missing issue as an answer, not an error', async () => {
    const { link } = renderApp({ initialPath: detailPath(issueId(404)) })

    // `issue(id:)` is nullable in the schema, so null is a successful
    // response meaning the row is not there.
    await link.resolve('IssueDetail', { data: issueDetailData(null) })

    const panel = inspector()
    expect(
      within(panel).getByRole('heading', { level: 2, name: 'Issue not found' }),
    ).toBeInTheDocument()
    expect(within(panel).queryByRole('alert')).toBeNull()

    // With a way back, which is what this state needs and a retry is not.
    expect(
      within(panel).getByRole('button', { name: 'Back to issues' }),
    ).toBeInTheDocument()
  })

  it('answers a malformed id without asking the server', async () => {
    const { link } = renderApp({ initialPath: detailPath('not-a-uuid') })

    await link.idle()

    // A malformed id would fail at variable coercion -- a top-level GraphQL
    // error -- and put a "something went wrong" panel in front of someone
    // whose actual situation is that the issue does not exist.
    expect(link.countOf('IssueDetail')).toBe(0)
    expect(
      within(inspector()).getByRole('heading', { level: 2, name: 'Issue not found' }),
    ).toBeInTheDocument()
  })

  it('reports a failed detail request as an error, with a retry', async () => {
    const { link, user } = renderApp({ initialPath: detailPath(ALPHA_ID) })

    await link.fail('IssueDetail', new Error('Failed to fetch'))

    const alert = within(inspector()).getByRole('alert')
    expect(alert).toHaveTextContent('Could not load this issue')
    expect(alert).toHaveTextContent('Failed to fetch')

    await user.click(within(alert).getByRole('button', { name: 'Try again' }))
    expect(link.countOf('IssueDetail')).toBe(2)

    await link.resolve('IssueDetail', {
      data: issueDetailData(issueDetail(1, { title: 'Alpha' })),
    })

    expect(within(inspector()).queryByRole('alert')).toBeNull()
  })

  it('shows a loading state before the issue arrives', async () => {
    const { link } = renderApp({ initialPath: detailPath(ALPHA_ID) })

    await link.waitForRequest('IssueDetail')

    const status = within(inspector()).getByRole('status')
    expect(status).toHaveAttribute('aria-busy', 'true')
    expect(status).toHaveTextContent('Loading issue')

    // "Not found" must not flash at every visitor while the request is still
    // in flight.
    expect(screen.queryByText('Issue not found')).toBeNull()
  })

  it('resolves the ids the schema hands back into people and statuses', async () => {
    const { link } = renderApp()

    await link.resolve('IssueWorkspaceContext', { data: workspaceContextData() })
    await link.resolve('IssueList', {
      data: issueListData([
        issueRow(1, {
          title: 'Alpha',
          assigneeId: '00000000-0000-4000-8000-00000000aa01',
        }),
      ]),
    })

    /*
      `Issue.assigneeId` and `Issue.workflowStateId` are raw UUIDs -- there is
      no `Issue.assignee` object in the schema -- so a row that names a person
      or a status has resolved it through the workspace context. Asserting the
      accessible name is asserting that the lookup happened: a regression that
      dropped it would render the UUID, or nothing.
    */
    const [row] = issueRows()
    expect(row).toHaveAccessibleName(/Ada Lovelace/)
    expect(row).toHaveAccessibleName(/Status: Todo/)
  })
})
