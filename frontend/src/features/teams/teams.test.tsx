import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { main, renderApp } from '../../test/render'
import {
  TEAM_ID,
  WORKSPACE_SLUG,
  issueListData,
  issueRow,
  workspaceContextData,
} from '../../test/factories'
import type { TeamBoardsQuery } from '../../generated/operations'

/**
 * The two team screens, against the real router and a controlled network.
 *
 * The risk on both is the same and it is not the rendering: the URL names a
 * team by **key** and every query needs an **id**, so a resolution step sits
 * between the address bar and the request. Three of its four outcomes look
 * alike on screen -- no team -- and mean completely different things.
 */

const OTHER_TEAM_ID = '00000000-0000-4000-8000-00000000ee02'

const teamsData: TeamBoardsQuery = {
  teams: [
    {
      __typename: 'Team',
      id: TEAM_ID,
      key: 'ENG',
      name: 'Engineering',
      createdAt: '2026-01-01T00:00:00.000Z',
      workflowStates: [
        {
          __typename: 'WorkflowState',
          id: '00000000-0000-4000-8000-00000000fa02',
          name: 'Shipped',
          category: 'COMPLETED',
          color: '#1a7f4b',
          position: 1,
        },
        {
          __typename: 'WorkflowState',
          id: '00000000-0000-4000-8000-00000000fa01',
          name: 'Inbox',
          category: 'BACKLOG',
          color: null,
          position: 0,
        },
      ],
    },
    {
      __typename: 'Team',
      id: OTHER_TEAM_ID,
      key: 'DES',
      name: 'Design',
      createdAt: '2026-01-01T00:00:00.000Z',
      workflowStates: [],
    },
  ],
}

describe('resolving a team key', () => {
  it('sends the resolved id, not the key, to issues(teamId:)', async () => {
    const view = renderApp({ initialPath: `/${WORKSPACE_SLUG}/team/ENG/issues` })

    await view.link.resolve('TeamBoards', { data: teamsData })

    await expect(view.link.waitForRequest('TeamIssueList')).resolves.toMatchObject({
      workspaceSlug: WORKSPACE_SLUG,
      teamId: TEAM_ID,
      after: null,
    })
  })

  it('matches a lowercased key from a hand-typed URL', async () => {
    const view = renderApp({ initialPath: `/${WORKSPACE_SLUG}/team/eng` })

    await view.link.resolve('TeamBoards', { data: teamsData })

    // Widening only: the schema guarantees keys are uppercase and unique
    // within the workspace, so an uppercase comparison cannot make two teams
    // collide.
    expect(within(main()).getByRole('heading', { level: 1 })).toHaveTextContent(
      'Engineering',
    )
  })

  it('gives a key that names no team a clean not-found', async () => {
    const view = renderApp({ initialPath: `/${WORKSPACE_SLUG}/team/NOPE` })

    await view.link.resolve('TeamBoards', { data: teamsData })

    expect(within(main()).getByRole('heading', { level: 1 })).toHaveTextContent(
      'Team not found',
    )
    // Not a half-drawn screen: no board, no issue list, and nothing was
    // asked about a team that does not exist.
    expect(within(main()).queryByRole('list')).not.toBeInTheDocument()
    expect(view.link.countOf('TeamIssueList')).toBe(0)
    expect(within(main()).getByRole('link', { name: 'Back to issues' })).toBeInTheDocument()
  })

  it('does not call a failed request a missing team', async () => {
    const view = renderApp({ initialPath: `/${WORKSPACE_SLUG}/team/ENG` })

    await view.link.fail('TeamBoards', new Error('Network unreachable'))

    // The distinction this union exists for. "Team not found" sends the user
    // looking for a team that is there.
    expect(within(main()).getByRole('heading', { level: 1 })).toHaveTextContent('Team')
    expect(
      within(main()).getByText("Could not load this workspace's teams"),
    ).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Try again|Retry/i })).toBeInTheDocument()
    expect(within(main()).queryByText('Team not found')).not.toBeInTheDocument()
  })
})

describe('the team overview', () => {
  it('lists the board in server order with the category behind each name', async () => {
    const view = renderApp({ initialPath: `/${WORKSPACE_SLUG}/team/ENG` })

    await view.link.resolve('TeamBoards', { data: teamsData })
    await view.link.resolve('TeamIssueList', { data: issueListData([]) })
    // `IssueWorkspaceContext` is left unanswered on purpose. It selects the
    // same `teams { workflowStates }` and writes the same cache entry, so
    // answering it here would decide which fixture wins rather than what the
    // screen does with one.

    const board = within(main()).getByRole('list', { name: 'ENG workflow states' })
    const rows = within(board).getAllByRole('listitem')

    // Sorted by `position`, not by the order the server happened to send: the
    // fixture deliberately lists Shipped (1) before Inbox (0).
    expect(rows[0]).toHaveTextContent('Inbox')
    expect(rows[1]).toHaveTextContent('Shipped')

    // The name belongs to the team -- a state called "Inbox" is a backlog
    // state -- so the category is shown beside it and never inferred.
    expect(within(rows[0]!).getByText('Backlog')).toBeInTheDocument()
    expect(within(rows[1]!).getByText('Completed')).toBeInTheDocument()
  })
})

describe('team issues', () => {
  it('shows the team’s issues with no "among those loaded" caveat', async () => {
    const view = renderApp({ initialPath: `/${WORKSPACE_SLUG}/team/ENG/issues` })

    await view.link.resolve('TeamBoards', { data: teamsData })
    await view.link.resolve('TeamIssueList', {
      data: issueListData([issueRow(1), issueRow(2)]),
    })
    await view.link.resolve('IssueWorkspaceContext', { data: workspaceContextData() })

    expect(within(main()).getByRole('list', { name: 'ENG issues' })).toBeInTheDocument()

    // `issues(teamId:)` is a real server-side filter, so this list is
    // complete -- unlike My Issues and a project's issues, which say so.
    expect(within(main()).queryByText(/among those loaded|matched in the browser/i)).toBeNull()
    expect(within(main()).getByText(/End of list/)).toBeInTheDocument()
  })
})
