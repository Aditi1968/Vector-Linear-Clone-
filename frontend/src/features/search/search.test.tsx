import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { main, renderApp } from '../../test/render'
import { WORKSPACE_SLUG, workspaceContextData } from '../../test/factories'
import type { WorkspaceSearchQuery } from '../../generated/operations'

/**
 * The search screen, against the real router and a controlled network.
 *
 * Two risks, and both are about the URL rather than about matching.
 *
 *   1. The query has to survive a reload and a paste, which means it lives in
 *      the address bar and the request is driven from there. If the request
 *      were driven from component state instead, everything below would still
 *      pass except the round trip -- so the round trip is what is asserted:
 *      typing changes the URL, and the URL is what produces the request.
 *   2. The backend resolves "ENG-42" to that issue directly. Any tidying of
 *      the input on this side -- uppercasing, stripping the hyphen, splitting
 *      into terms -- breaks the one query this is best at.
 */

const ISSUE_ID = '00000000-0000-4000-8000-0000000000d1'
const PROJECT_ID = '00000000-0000-4000-8000-0000000000d2'

const results: WorkspaceSearchQuery = {
  search: {
    __typename: 'SearchResults',
    issues: [
      {
        __typename: 'Issue',
        id: ISSUE_ID,
        identifier: 'ENG-42',
        title: 'Payments time out',
        priority: 1,
        teamId: '00000000-0000-4000-8000-00000000ee01',
        workflowStateId: '00000000-0000-4000-8000-00000000ff01',
        assigneeId: null,
      },
    ],
    projects: [
      {
        __typename: 'Project',
        id: PROJECT_ID,
        name: 'Payments migration',
        state: 'STARTED',
        targetDate: '2026-03-14',
        updatedAt: '2026-01-01T00:00:00.000Z',
      },
    ],
  },
}

const empty: WorkspaceSearchQuery = {
  search: { __typename: 'SearchResults', issues: [], projects: [] },
}

describe('a search that arrived as a URL', () => {
  it('runs the query in ?q= without anyone typing', async () => {
    const view = renderApp({
      initialPath: `/${WORKSPACE_SLUG}/search?q=payments`,
    })

    // This is what makes a search linkable and survivable across a reload.
    await expect(view.link.waitForRequest('WorkspaceSearch')).resolves.toMatchObject({
      workspaceSlug: WORKSPACE_SLUG,
      query: 'payments',
    })

    expect(screen.getByLabelText('Search')).toHaveValue('payments')
  })

  it('passes an identifier through untouched', async () => {
    const view = renderApp({ initialPath: `/${WORKSPACE_SLUG}/search?q=ENG-42` })

    // Not "eng 42", not "ENG42". The backend resolves this to one issue and
    // any tidying here would break it.
    await expect(view.link.waitForRequest('WorkspaceSearch')).resolves.toMatchObject({
      query: 'ENG-42',
    })
  })

  it('sends nothing at all when there is no query', async () => {
    const view = renderApp({ initialPath: `/${WORKSPACE_SLUG}/search` })

    await view.link.idle()

    expect(view.link.countOf('WorkspaceSearch')).toBe(0)
    expect(within(main()).getByText('Type to search.')).toBeInTheDocument()
  })
})

describe('typing', () => {
  it('reaches the network only through the URL', async () => {
    const view = renderApp({ initialPath: `/${WORKSPACE_SLUG}/search?q=pay` })

    await view.link.resolve('WorkspaceSearch', { data: empty })

    await view.user.type(screen.getByLabelText('Search'), 'ments')

    // `useSearch` reads the URL and never the input, so a second request can
    // only mean the URL moved. Debounced, so this is one request and not five.
    await expect(view.link.waitForRequest('WorkspaceSearch')).resolves.toMatchObject({
      query: 'payments',
    })
    expect(view.link.countOf('WorkspaceSearch')).toBe(2)
  })
})

describe('results', () => {
  it('presents issues and projects as separate lists', async () => {
    const view = renderApp({ initialPath: `/${WORKSPACE_SLUG}/search?q=payments` })

    await view.link.resolve('WorkspaceSearch', { data: results })
    await view.link.resolve('IssueWorkspaceContext', { data: workspaceContextData() })

    const issues = within(main()).getByRole('list', { name: 'Matching issues' })
    const projects = within(main()).getByRole('list', { name: 'Matching projects' })

    expect(within(issues).getByRole('link')).toHaveAttribute(
      'href',
      `/${WORKSPACE_SLUG}/issues/${ISSUE_ID}`,
    )
    expect(within(projects).getByRole('link')).toHaveAttribute(
      'href',
      `/${WORKSPACE_SLUG}/projects/${PROJECT_ID}`,
    )

    // A live region, so the outcome is announced rather than silently
    // replacing what was on screen.
    expect(within(main()).getByText('2 results for "payments".')).toBeInTheDocument()
  })

  it('says the list is the top matches rather than everything', async () => {
    const view = renderApp({ initialPath: `/${WORKSPACE_SLUG}/search?q=payments` })

    await view.link.resolve('WorkspaceSearch', { data: results })

    // `search` returns two plain lists and no `pageInfo`, so a list that
    // stops at twenty with no explanation looks like a list that ends there.
    expect(
      within(main()).getByText(/The closest matches, up to twenty of each/),
    ).toBeInTheDocument()
  })

  it('distinguishes "nothing matched" from "nothing typed"', async () => {
    const view = renderApp({ initialPath: `/${WORKSPACE_SLUG}/search?q=zzz` })

    await view.link.resolve('WorkspaceSearch', { data: empty })

    expect(within(main()).getByText('Nothing matched')).toBeInTheDocument()
    expect(within(main()).queryByText('Type to search.')).toBeNull()
  })

  it('reports a failure with a retry rather than an empty result', async () => {
    const view = renderApp({ initialPath: `/${WORKSPACE_SLUG}/search?q=payments` })

    await view.link.fail('WorkspaceSearch', new Error('Network unreachable'))

    expect(within(main()).getByText('Search failed')).toBeInTheDocument()
    expect(within(main()).queryByText('Nothing matched')).toBeNull()
    expect(screen.getByRole('button', { name: /Try again|Retry/i })).toBeInTheDocument()
  })
})
