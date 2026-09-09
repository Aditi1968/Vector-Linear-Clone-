import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { main, renderApp } from '../../test/render'
import {
  TEAM_ID,
  TODO_STATE_ID,
  WORKSPACE_SLUG,
  workspaceContextData,
} from '../../test/factories'
import type { TriageQueueQuery } from '../../generated/operations'

/**
 * The triage screen, against the real router, cache and a controlled network.
 *
 * Behaviour through role and label queries: what the screen sends, what it
 * draws, and -- the two that matter most here -- what it refuses to draw
 * because the schema does not carry it, and what it refuses to send before it
 * knows a team.
 */

const ISSUE_ONE = '00000000-0000-4000-8000-0000000a0001'
const ISSUE_TWO = '00000000-0000-4000-8000-0000000a0002'

const TRIAGE_PATH = `/${WORKSPACE_SLUG}/triage`

function triageRow(
  id: string,
  identifier: string,
  title: string,
  priority = 0,
): TriageQueueQuery['triageIssues']['nodes'][number] {
  return {
    __typename: 'TriageIssue',
    enteredAt: new Date(Date.now() - 60 * 60 * 1000).toISOString(),
    issue: {
      __typename: 'IssueSummary',
      id,
      identifier,
      title,
      description: null,
      priority,
    },
  }
}

function queueData(
  nodes: TriageQueueQuery['triageIssues']['nodes'] = [],
  { hasNextPage = false, triageCount = nodes.length } = {},
): TriageQueueQuery {
  return {
    triageCount,
    triageIssues: {
      __typename: 'TriageIssueConnection',
      nodes,
      pageInfo: {
        __typename: 'PageInfo',
        hasNextPage,
        endCursor: hasNextPage ? 'cursor-1' : null,
      },
    },
  }
}

/** Mount the screen and answer the shared workspace context. */
async function openTriage(data: TriageQueueQuery | null = queueData()) {
  const view = renderApp({ initialPath: TRIAGE_PATH })

  await view.link.resolve('IssueWorkspaceContext', { data: workspaceContextData() })

  if (data !== null) {
    await view.link.resolve('TriageQueue', { data })
  }

  return view
}

describe('the triage queue', () => {
  it('cannot ask for a queue before it knows a team', async () => {
    const view = renderApp({ initialPath: TRIAGE_PATH })

    await view.link.waitForRequest('IssueWorkspaceContext')

    // `triageIssues(teamId:)` requires a team, so the query is skipped rather
    // than sent with a placeholder -- a `UUID!` coerced from `''` would be a
    // top-level error reported to the user as "something went wrong".
    expect(view.link.countOf('TriageQueue')).toBe(0)
  })

  it('asks for the first team once the context arrives', async () => {
    const view = renderApp({ initialPath: TRIAGE_PATH })

    await view.link.resolve('IssueWorkspaceContext', { data: workspaceContextData() })

    await expect(view.link.waitForRequest('TriageQueue')).resolves.toEqual({
      workspaceSlug: WORKSPACE_SLUG,
      teamId: TEAM_ID,
      after: null,
    })
  })

  it('lists what is waiting, oldest first', async () => {
    await openTriage(
      queueData([
        triageRow(ISSUE_ONE, 'ENG-1', 'Login button does nothing'),
        triageRow(ISSUE_TWO, 'ENG-2', 'Export times out'),
      ]),
    )

    const list = within(main()).getByRole('list', { name: /issues waiting/i })

    expect(within(list).getByText('Login button does nothing')).toBeInTheDocument()
    expect(within(list).getByText('Export times out')).toBeInTheDocument()
  })

  it('says how many are waiting rather than letting a page read as the queue', async () => {
    await openTriage(
      queueData([triageRow(ISSUE_ONE, 'ENG-1', 'Login button does nothing')], {
        hasNextPage: true,
        triageCount: 41,
      }),
    )

    /*
      `TriageIssueConnection` has no `totalCount` and this screen loads one
      page, so "1 loaded" and "41 waiting" are different facts. Stating only
      the first would make a page of 25 read as the whole queue.

      Said in two places now, which is why this asserts them separately
      rather than matching /41/ anywhere: the header's mono readout carries
      the queue's whole length, and the line above the list says how much of
      it is on screen. A bare /41/ would match both and throw.
    */
    expect(within(main()).getByText('41 waiting')).toBeInTheDocument()
    expect(within(main()).getByText(/oldest of/)).toHaveTextContent('41')
  })

  it('says a team has nothing waiting rather than showing an empty list', async () => {
    await openTriage(queueData([]))

    expect(within(main()).getByText(/Nothing waiting for ENG/)).toBeInTheDocument()
  })

  it('offers a retry when the queue could not be loaded', async () => {
    const view = renderApp({ initialPath: TRIAGE_PATH })

    await view.link.resolve('IssueWorkspaceContext', { data: workspaceContextData() })
    await view.link.fail('TriageQueue', new Error('Network unreachable'))

    // `ErrorState` is a `role="alert"` region, not a heading -- an error that
    // arrives after the page has settled has to announce itself.
    const alert = await screen.findByRole('alert')

    expect(alert).toHaveTextContent('Could not load the triage queue')

    await view.user.click(within(alert).getByRole('button', { name: 'Try again' }))

    // A retry is the same question asked again, not a different one.
    await expect(view.link.waitForRequest('TriageQueue')).resolves.toEqual({
      workspaceSlug: WORKSPACE_SLUG,
      teamId: TEAM_ID,
      after: null,
    })
  })
})

describe('deciding on a triage issue', () => {
  it('accepts into the state that was chosen, never a guessed one', async () => {
    const view = await openTriage(
      queueData([triageRow(ISSUE_ONE, 'ENG-1', 'Login button does nothing')]),
    )

    await view.user.click(
      screen.getByRole('button', { name: 'Accept ENG-1' }),
    )

    /*
      Every state of the team's board is offered. `triageAccept` requires a
      `workflowStateId` and the schema declares no default, because "accepted"
      is not a state -- it is whichever column the person picked.
    */
    await view.user.click(screen.getByRole('menuitem', { name: 'Todo' }))

    await expect(view.link.waitForRequest('TriageAccept')).resolves.toEqual({
      input: {
        workspaceSlug: WORKSPACE_SLUG,
        issueId: ISSUE_ONE,
        workflowStateId: TODO_STATE_ID,
      },
    })
  })

  it('reloads the queue after a decision, because the row has left it', async () => {
    const view = await openTriage(
      queueData([triageRow(ISSUE_ONE, 'ENG-1', 'Login button does nothing')]),
    )

    await view.user.click(
      screen.getByRole('button', { name: 'More actions on ENG-1' }),
    )
    await view.user.click(screen.getByRole('menuitem', { name: 'Decline' }))

    await view.link.resolve('TriageDecline', {
      data: {
        triageDecline: {
          __typename: 'TriagePayload',
          issue: { __typename: 'Issue', id: ISSUE_ONE },
          errors: [],
        },
      },
    })

    /*
      Membership of a server-filtered connection is not a property of any
      entity in it, so normalising the returned `Issue` cannot take the row
      out of the list. Only a refetch can.
    */
    expect(view.link.countOf('TriageQueue')).toBe(2)
  })

  it('reports a refusal instead of pretending the decision landed', async () => {
    const view = await openTriage(
      queueData([triageRow(ISSUE_ONE, 'ENG-1', 'Login button does nothing')]),
    )

    await view.user.click(
      screen.getByRole('button', { name: 'More actions on ENG-1' }),
    )
    await view.user.click(screen.getByRole('menuitem', { name: 'Decline' }))

    await view.link.resolve('TriageDecline', {
      data: {
        triageDecline: {
          __typename: 'TriagePayload',
          issue: null,
          errors: [
            {
              __typename: 'ValidationErrorType',
              field: 'issueId',
              code: 'NOT_FOUND',
              message: 'That issue is no longer in triage.',
            },
          ],
        },
      },
    })

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'That issue is no longer in triage.',
    )
  })

  it('sets a priority through issueUpdate rather than a triage mutation', async () => {
    const view = await openTriage(
      queueData([triageRow(ISSUE_ONE, 'ENG-1', 'Login button does nothing')]),
    )

    await view.user.click(
      screen.getByRole('button', { name: 'More actions on ENG-1' }),
    )
    await view.user.click(screen.getByRole('menuitem', { name: 'Urgent' }))

    /*
      There is no `triagePrioritise` in the schema, deliberately: prioritising
      an issue is the same act wherever it is done. Only the changed field is
      sent -- an omitted `assigneeId` leaves the assignee alone, where an
      explicit null would clear it.
    */
    await expect(view.link.waitForRequest('TriageIssueUpdate')).resolves.toEqual({
      id: ISSUE_ONE,
      input: { workspaceSlug: WORKSPACE_SLUG, priority: 1 },
    })
  })

  it('does not claim a status or an assignee it was never sent', async () => {
    await openTriage(
      queueData([triageRow(ISSUE_ONE, 'ENG-1', 'Login button does nothing')]),
    )

    const list = within(main()).getByRole('list', { name: /issues waiting/i })

    /*
      `TriageIssue.issue` is an `IssueSummary`: no `workflowStateId`, no
      `assigneeId`. A row that rendered "Todo" or "Unassigned" here would be
      stating something the server never told it -- the same class of lie as
      "no results" from an index that was simply empty. The row draws a
      priority, a key, a title and an age, and leaves the rest alone.
    */
    expect(within(list).queryByText('Todo')).toBeNull()
    expect(within(list).queryByText('Done')).toBeNull()
    expect(within(list).queryByText(/unassigned/i)).toBeNull()
  })
})
