import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { main, renderApp } from '../../test/render'
import {
  MEMBER_ID,
  TEAM_ID,
  TODO_STATE_ID,
  WORKSPACE_SLUG,
  workspaceContextData,
} from '../../test/factories'
import type {
  SavedViewFieldsFragment,
  SavedViewListQuery,
  SavedViewResultsQuery,
} from '../../generated/operations'

/**
 * The saved-views screen, against the real router, cache and a controlled
 * network.
 *
 * The claim worth testing hardest is the one about the filter asymmetry: a
 * stored filter that says "unassigned" cannot be sent back through
 * `IssueFilterInput`, and this screen must refuse to rewrite it rather than
 * silently widening the view. That is ./lib/savedViews.ts's rule, exercised
 * here end to end through the form.
 */

const VIEW_ONE = '00000000-0000-4000-8000-0000000b0001'
const VIEW_TWO = '00000000-0000-4000-8000-0000000b0002'
const ISSUE_ONE = '00000000-0000-4000-8000-0000000c0001'

const VIEWS_PATH = `/${WORKSPACE_SLUG}/saved-views`

function emptyFilter(): SavedViewFieldsFragment['filter'] {
  return {
    __typename: 'SavedViewFilter',
    teamId: null,
    workflowStateId: null,
    stateCategory: null,
    labelId: null,
    priority: null,
    assignee: null,
    project: null,
    cycle: null,
  }
}

function view(overrides: Partial<SavedViewFieldsFragment> = {}): SavedViewFieldsFragment {
  return {
    __typename: 'SavedView',
    id: VIEW_ONE,
    name: 'My open bugs',
    teamId: null,
    filter: emptyFilter(),
    orderField: 'CREATED_AT',
    orderDirection: 'DESC',
    layout: 'LIST',
    grouping: null,
    subgrouping: null,
    visibility: 'PERSONAL',
    ...overrides,
  }
}

function listData(views: readonly SavedViewFieldsFragment[]): SavedViewListQuery {
  return {
    savedViews: {
      __typename: 'SavedViewConnection',
      nodes: [...views],
      pageInfo: { __typename: 'PageInfo', hasNextPage: false, endCursor: null },
    },
  }
}

function resultsData(
  nodes: SavedViewResultsQuery['savedView'] extends null
    ? never
    : NonNullable<SavedViewResultsQuery['savedView']>['issues']['nodes'] = [],
): SavedViewResultsQuery {
  return {
    savedView: {
      __typename: 'SavedView',
      id: VIEW_ONE,
      name: 'My open bugs',
      issues: {
        __typename: 'IssueConnection',
        nodes,
        pageInfo: { __typename: 'PageInfo', hasNextPage: false, endCursor: null },
        totalCount: nodes.length,
      },
    },
  }
}

function resultIssue(title: string) {
  return {
    __typename: 'Issue' as const,
    id: ISSUE_ONE,
    identifier: 'ENG-9',
    title,
    priority: 1,
    workflowStateId: TODO_STATE_ID,
    assigneeId: MEMBER_ID,
    dueDate: null,
  }
}

/** Mount the screen with a list of views and the first one's results. */
async function openViews(
  views: readonly SavedViewFieldsFragment[] = [view()],
  results: SavedViewResultsQuery = resultsData([]),
) {
  const app = renderApp({ initialPath: VIEWS_PATH })

  await app.link.resolve('IssueWorkspaceContext', { data: workspaceContextData() })
  await app.link.resolve('SavedViewList', { data: listData(views) })

  if (views.length > 0) {
    await app.link.resolve('SavedViewResults', { data: results })
  }

  return app
}

describe('the saved-view list', () => {
  it('lists the views the viewer can see', async () => {
    await openViews([
      view(),
      view({ id: VIEW_TWO, name: 'Team backlog', visibility: 'SHARED' }),
    ])

    const list = within(main()).getByRole('list', { name: 'Saved views' })

    expect(within(list).getByRole('button', { name: /^My open bugs/ })).toBeInTheDocument()
    expect(within(list).getByRole('button', { name: /^Team backlog/ })).toBeInTheDocument()
  })

  it('says a view is shared rather than leaving visibility invisible', async () => {
    await openViews([view({ visibility: 'SHARED' })])

    expect(within(main()).getByText('Shared')).toBeInTheDocument()
  })

  it('offers to create the first view when there are none', async () => {
    await openViews([])

    expect(within(main()).getByText('No saved views yet')).toBeInTheDocument()
    expect(
      within(main()).getByRole('button', { name: 'Create the first view' }),
    ).toBeInTheDocument()
  })

  it('offers a retry when the list could not be loaded', async () => {
    const app = renderApp({ initialPath: VIEWS_PATH })

    await app.link.resolve('IssueWorkspaceContext', { data: workspaceContextData() })
    await app.link.fail('SavedViewList', new Error('Network unreachable'))

    const alert = await screen.findByRole('alert')

    expect(alert).toHaveTextContent('Could not load saved views')

    await app.user.click(within(alert).getByRole('button', { name: 'Try again' }))

    await expect(app.link.waitForRequest('SavedViewList')).resolves.toEqual({
      workspaceSlug: WORKSPACE_SLUG,
      after: null,
    })
  })
})

describe("a view's results", () => {
  it('asks the server what the stored filter selects', async () => {
    const app = renderApp({ initialPath: VIEWS_PATH })

    await app.link.resolve('IssueWorkspaceContext', { data: workspaceContextData() })
    await app.link.resolve('SavedViewList', { data: listData([view()]) })

    /*
      `SavedView.issues` is the only way to ask this: the filter and the
      ordering come from the stored row, never from the document, so the page
      that comes back is the one that was saved. Nothing here re-sends a
      filter.
    */
    await expect(app.link.waitForRequest('SavedViewResults')).resolves.toEqual({
      workspaceSlug: WORKSPACE_SLUG,
      id: VIEW_ONE,
      after: null,
    })
  })

  it('draws a status and an assignee, which a full Issue does carry', async () => {
    await openViews([view()], resultsData([resultIssue('Export times out')]))

    const panel = within(main()).getByRole('complementary', { name: 'Issues in My open bugs' })

    expect(within(panel).getByText('Export times out')).toBeInTheDocument()

    // Unlike a triage row, these nodes are full `Issue` objects, so
    // `workflowStateId` and `assigneeId` are present and resolvable.
    expect(within(panel).getByText('Ada Lovelace')).toBeInTheDocument()
  })

  it('separates "the filter matches nothing" from "the view is gone"', async () => {
    await openViews([view()], resultsData([]))

    expect(within(main()).getByText('Nothing matches this view')).toBeInTheDocument()
  })

  it('gives one answer to a deleted view and to one the viewer may not see', async () => {
    await openViews([view()], { savedView: null })

    /*
      The server answers null for both on purpose, so that a refusal cannot be
      used to probe for what exists. The screen does not guess which it was.
    */
    expect(
      within(main()).getByText('This view is no longer available'),
    ).toBeInTheDocument()
  })
})

describe('editing a saved view', () => {
  it('sends only the fields the form collected', async () => {
    const app = await openViews([view()])

    await app.user.click(screen.getByRole('button', { name: 'Actions on My open bugs' }))
    await app.user.click(screen.getByRole('menuitem', { name: 'Edit view...' }))

    const name = await screen.findByLabelText('Name')

    await app.user.clear(name)
    await app.user.type(name, 'Renamed')
    await app.user.click(screen.getByRole('button', { name: 'Save changes' }))

    await expect(app.link.waitForRequest('SavedViewUpdate')).resolves.toEqual({
      input: {
        workspaceSlug: WORKSPACE_SLUG,
        id: VIEW_ONE,
        name: 'Renamed',
        teamId: null,
        visibility: 'PERSONAL',
        layout: 'LIST',
        grouping: null,
        orderBy: { field: 'CREATED_AT', direction: 'DESC' },
        /*
          All eight columns, because this view had a stored filter and every
          one of them was read back out of it. An empty stored filter is still
          a filter that round-trips -- the five the form cannot draw are
          re-sent as the nulls they were, not dropped.
        */
        filter: {
          teamId: null,
          workflowStateId: null,
          stateCategory: null,
          labelId: null,
          priority: null,
          assigneeId: null,
          projectId: null,
          cycleId: null,
        },
      },
    })
  })

  it('carries a filter it cannot draw a control for back out unchanged', async () => {
    const app = await openViews([
      view({
        filter: {
          ...emptyFilter(),
          // No control renders these two. They must survive a rename.
          workflowStateId: TODO_STATE_ID,
          project: { __typename: 'SavedViewIdFilter', id: '00000000-0000-4000-8000-0000000d0001' },
        },
      }),
    ])

    await app.user.click(screen.getByRole('button', { name: 'Actions on My open bugs' }))
    await app.user.click(screen.getByRole('menuitem', { name: 'Edit view...' }))

    const name = await screen.findByLabelText('Name')

    await app.user.clear(name)
    await app.user.type(name, 'Renamed')
    await app.user.click(screen.getByRole('button', { name: 'Save changes' }))

    const sent = await app.link.waitForRequest('SavedViewUpdate')
    const input = (sent as { input: { filter: Record<string, unknown> } }).input

    /*
      `IssueTemplateFieldsInput`'s sibling problem: an update that dropped
      these would not "leave them alone", it would clear them. The form has no
      control for either and re-sends both.
    */
    expect(input.filter.workflowStateId).toBe(TODO_STATE_ID)
    expect(input.filter.projectId).toBe('00000000-0000-4000-8000-0000000d0001')
  })

  it('refuses to rewrite a filter the input type cannot express', async () => {
    const app = await openViews([
      view({
        filter: {
          ...emptyFilter(),
          // "Unassigned". `SavedViewFilter` can say this; `IssueFilterInput`
          // has a flat nullable `assigneeId` and cannot.
          assignee: { __typename: 'SavedViewIdFilter', id: null },
        },
      }),
    ])

    await app.user.click(screen.getByRole('button', { name: 'Actions on My open bugs' }))
    await app.user.click(screen.getByRole('menuitem', { name: 'Edit view...' }))

    const name = await screen.findByLabelText('Name')

    // The reason is on screen, not merely implied by a disabled control.
    expect(screen.getByRole('note')).toHaveTextContent(/unassigned, no project or no cycle/i)

    await app.user.clear(name)
    await app.user.type(name, 'Renamed')
    await app.user.click(screen.getByRole('button', { name: 'Save changes' }))

    const sent = await app.link.waitForRequest('SavedViewUpdate')
    const input = (sent as { input: Record<string, unknown> }).input

    /*
      `filter` is absent from the patch entirely -- not null, not a
      reconstruction. `SavedViewUpdateInput` leaves an omitted field alone, so
      the stored "unassigned" survives. Sending a rebuilt filter here would
      quietly turn "unassigned" into "any assignee" and widen the view.
    */
    expect(input).not.toHaveProperty('filter')
    expect(input['name']).toBe('Renamed')
  })

  it('reloads a view\'s results after an edit, because the filter may have moved', async () => {
    const app = await openViews([view()])

    await app.user.click(screen.getByRole('button', { name: 'Actions on My open bugs' }))
    await app.user.click(screen.getByRole('menuitem', { name: 'Edit view...' }))

    const name = await screen.findByLabelText('Name')

    await app.user.clear(name)
    await app.user.type(name, 'Renamed')
    await app.user.click(screen.getByRole('button', { name: 'Save changes' }))

    await app.link.resolve('SavedViewUpdate', {
      data: {
        savedViewUpdate: {
          __typename: 'SavedViewPayload',
          savedView: view({ name: 'Renamed' }),
          errors: [],
        },
      },
    })

    /*
      Which issues a view selects is a server-side answer. Normalising the
      view's own fields cannot recompute the membership of `SavedView.issues`.
    */
    expect(app.link.countOf('SavedViewResults')).toBe(2)
  })

  it('shows a refused name against the field the server named', async () => {
    const app = await openViews([view()])

    await app.user.click(screen.getByRole('button', { name: 'Actions on My open bugs' }))
    await app.user.click(screen.getByRole('menuitem', { name: 'Edit view...' }))

    await screen.findByLabelText('Name')
    await app.user.click(screen.getByRole('button', { name: 'Save changes' }))

    await app.link.resolve('SavedViewUpdate', {
      data: {
        savedViewUpdate: {
          __typename: 'SavedViewPayload',
          savedView: null,
          errors: [
            {
              __typename: 'ValidationErrorType',
              field: 'name',
              code: 'BAD_USER_INPUT',
              message: 'A view with that name already exists.',
            },
          ],
        },
      },
    })

    const field = await screen.findByLabelText('Name')

    expect(field).toHaveAccessibleDescription('A view with that name already exists.')
    expect(field).toBeInvalid()
  })
})

describe('creating a saved view', () => {
  it('sends the composed view and refetches the list', async () => {
    const app = await openViews([])

    await app.user.click(screen.getByRole('button', { name: 'Create the first view' }))

    await app.user.type(await screen.findByLabelText('Name'), 'Urgent work')
    await app.user.click(screen.getByRole('button', { name: 'Create view' }))

    await expect(app.link.waitForRequest('SavedViewCreate')).resolves.toEqual({
      input: {
        workspaceSlug: WORKSPACE_SLUG,
        name: 'Urgent work',
        teamId: null,
        visibility: 'PERSONAL',
        layout: 'LIST',
        grouping: null,
        orderBy: { field: 'CREATED_AT', direction: 'DESC' },
        // A new view has nothing to preserve, so only the three controls the
        // form renders are sent.
        filter: { teamId: null, stateCategory: null, priority: null },
      },
    })
  })

  it('files a new view against the team that was chosen', async () => {
    const app = await openViews([])

    await app.user.click(screen.getByRole('button', { name: 'Create the first view' }))

    await app.user.type(await screen.findByLabelText('Name'), 'ENG work')
    await app.user.selectOptions(screen.getByLabelText('Team'), TEAM_ID)
    await app.user.click(screen.getByRole('button', { name: 'Create view' }))

    const sent = await app.link.waitForRequest('SavedViewCreate')

    expect((sent as { input: { teamId: string } }).input.teamId).toBe(TEAM_ID)
  })
})
