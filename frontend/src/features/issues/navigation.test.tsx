import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import {
  issueDetail,
  issueDetailData,
  issueId,
  issueListData,
  issueRow,
  WORKSPACE_SLUG,
} from '../../test/factories'
import { issueRows, main, renderApp } from '../../test/render'

/**
 * Getting from the list to one issue, and back.
 *
 * The property worth protecting is that the *URL* is the state. Nothing is
 * handed from the list to the detail view; the id comes from the route and
 * the query runs from it, which is what makes a reload of
 * `/:workspaceSlug/issues/<id>` render the same page by the same path. A
 * test that only clicked a row and looked for a title would pass just as
 * well against an implementation that smuggled the issue across in memory
 * and broke on refresh.
 *
 * The workspace is part of that URL for the same reason the id is: the
 * server requires one on every field, so a detail request carries the slug
 * from the route and not from anywhere a reload would lose.
 */

const ALPHA_ID = issueId(1)

/** The list and one issue, inside the workspace every test here is in. */
const LIST_PATH = `/${WORKSPACE_SLUG}/issues`
const detailPath = (id: string) => `${LIST_PATH}/${id}`

describe('navigating to an issue', () => {
  it('opens the detail view from a row and puts the id in the URL', async () => {
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

    expect(screen.getByRole('heading', { level: 1, name: 'Alpha' })).toBeInTheDocument()
    expect(screen.getByText('The redirect loops.')).toBeInTheDocument()
    // The UUID is shown because it is the only identifier the product has.
    expect(within(main()).getByText(ALPHA_ID)).toBeInTheDocument()
  })

  it('renders the same page when the detail URL is opened directly', async () => {
    const { link } = renderApp({ initialPath: detailPath(ALPHA_ID) })

    const variables = await link.waitForRequest('IssueDetail')
    expect(variables).toEqual({ workspaceSlug: WORKSPACE_SLUG, id: ALPHA_ID })

    await link.resolve('IssueDetail', {
      data: issueDetailData(issueDetail(1, { title: 'Alpha' })),
    })

    expect(screen.getByRole('heading', { level: 1, name: 'Alpha' })).toBeInTheDocument()

    // No list request was needed to render an issue by URL.
    expect(link.countOf('IssueList')).toBe(0)
  })

  it('returns to the list without refetching it', async () => {
    const { link, user, currentPath } = renderApp()

    await link.resolve('IssueList', {
      data: issueListData([issueRow(1, { title: 'Alpha' })]),
    })

    await user.click(screen.getByRole('link', { name: /Alpha/ }))
    await link.resolve('IssueDetail', {
      data: issueDetailData(issueDetail(1, { title: 'Alpha' })),
    })

    await user.click(screen.getByRole('link', { name: /All issues/ }))

    expect(currentPath()).toBe(LIST_PATH)
    expect(screen.getByRole('link', { name: /Alpha/ })).toBeInTheDocument()

    /*
      Still one list request. The rows come back from the cache, and that
      matters beyond speed: a refetch sends `after: null`, which the merge
      policy reads as "start the list over" and which would drop every page
      loaded past the first.
    */
    expect(link.countOf('IssueList')).toBe(1)
  })

  it('treats a missing issue as an answer, not an error', async () => {
    const { link } = renderApp({ initialPath: detailPath(issueId(404)) })

    // `issue(id:)` is nullable in the schema, so null is a successful
    // response meaning the row is not there.
    await link.resolve('IssueDetail', { data: issueDetailData(null) })

    expect(
      screen.getByRole('heading', { level: 1, name: 'Issue not found' }),
    ).toBeInTheDocument()
    expect(within(main()).queryByRole('alert')).toBeNull()

    // With a way back, which is what this state needs and a retry is not.
    expect(screen.getByRole('link', { name: 'Back to issues' })).toBeInTheDocument()
  })

  it('answers a malformed id without asking the server', async () => {
    const { link } = renderApp({ initialPath: detailPath('not-a-uuid') })

    await link.idle()

    // A malformed id would fail at variable coercion -- a top-level GraphQL
    // error -- and put a "something went wrong" panel in front of someone
    // whose actual situation is that the issue does not exist.
    expect(link.countOf('IssueDetail')).toBe(0)
    expect(
      screen.getByRole('heading', { level: 1, name: 'Issue not found' }),
    ).toBeInTheDocument()
  })

  it('reports a failed detail request as an error, with a retry', async () => {
    const { link, user } = renderApp({ initialPath: detailPath(ALPHA_ID) })

    await link.fail('IssueDetail', new Error('Failed to fetch'))

    const alert = within(main()).getByRole('alert')
    expect(alert).toHaveTextContent('Could not load this issue')
    expect(alert).toHaveTextContent('Failed to fetch')

    await user.click(within(alert).getByRole('button', { name: 'Try again' }))
    expect(link.countOf('IssueDetail')).toBe(2)

    await link.resolve('IssueDetail', {
      data: issueDetailData(issueDetail(1, { title: 'Alpha' })),
    })

    expect(screen.getByRole('heading', { level: 1, name: 'Alpha' })).toBeInTheDocument()
    expect(within(main()).queryByRole('alert')).toBeNull()
  })

  it('shows a loading state before the issue arrives', async () => {
    const { link } = renderApp({ initialPath: detailPath(ALPHA_ID) })

    await link.waitForRequest('IssueDetail')

    const status = screen.getByRole('status')
    expect(status).toHaveAttribute('aria-busy', 'true')
    expect(status).toHaveTextContent('Loading issue')

    // "Not found" must not flash at every visitor while the request is still
    // in flight.
    expect(screen.queryByText('Issue not found')).toBeNull()
  })
})
