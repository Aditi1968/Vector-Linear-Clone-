import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import {
  MEMBER_ID,
  TEAM_ID,
  WORKSPACE_SLUG,
  issueId,
  issueListData,
  issueRow,
  validationError,
  workspaceContextData,
} from '../../test/factories'
import { main, renderApp } from '../../test/render'
import type {
  BoardIssueMoveMutation,
  IssueWorkspaceContextQuery,
} from '../../generated/operations'
import type { IssueRowFields, IssueValidationError } from '../issues/api'

/**
 * The board, against the real router, the real cache and a controlled network.
 *
 * The five claims worth a test here, chosen because each fails silently:
 *
 *   1. **Columns are the team's own workflow states, in `position` order.**
 *      The fixture's team calls them Icebox, Building and Shipped and returns
 *      them out of order on purpose. A board that had quietly grown a
 *      hardcoded Todo/Doing/Done would render perfectly well against a
 *      workspace whose teams happened to use those names.
 *   2. **A move is a real mutation, applied optimistically and rolled back.**
 *      The optimistic half is invisible if you only assert after the response,
 *      and the rollback is invisible unless the server refuses.
 *   3. **A card can be moved without a mouse.** A board that only works by
 *      dragging is one most people cannot use, and no rendering assertion
 *      would notice.
 *   4. **The view is in the URL, in both directions.** See ./lib/viewState.test.ts
 *      for the parsing; this is the half that proves the screen is wired to it.
 *   5. **The view reaches the server, and a filter nobody set is absent from
 *      the request.** Filtering and ordering are arguments now, so the request
 *      is where the view either works or quietly asks for something else --
 *      `assigneeId: null` is the unassigned issues, not all of them.
 *      ./lib/viewState.test.ts pins the mapping; this pins the wiring.
 *
 * There is deliberately no test that each card renders each of its fields.
 */

const ICEBOX = '00000000-0000-4000-8000-00000000ff10'
const BUILDING = '00000000-0000-4000-8000-00000000ff11'
const SHIPPED = '00000000-0000-4000-8000-00000000ff12'
const DESIGN_TEAM_ID = '00000000-0000-4000-8000-00000000ee02'

/**
 * A team that has renamed every one of its states, and a second team.
 *
 * The states are listed out of `position` order, which is the only way to tell
 * a board that sorts by position from one that renders the array as it
 * arrived -- the server's order and the board's order agree often enough that
 * a fixture in order proves nothing.
 */
function boardContext(): IssueWorkspaceContextQuery {
  return workspaceContextData({
    teams: [
      {
        __typename: 'Team',
        id: TEAM_ID,
        key: 'ENG',
        name: 'Engineering',
        workflowStates: [
          {
            __typename: 'WorkflowState',
            id: SHIPPED,
            name: 'Shipped',
            category: 'COMPLETED',
            position: 2,
            color: null,
          },
          {
            __typename: 'WorkflowState',
            id: ICEBOX,
            name: 'Icebox',
            category: 'BACKLOG',
            position: 0,
            color: null,
          },
          {
            __typename: 'WorkflowState',
            id: BUILDING,
            name: 'Building',
            category: 'STARTED',
            position: 1,
            color: null,
          },
        ],
      },
      {
        __typename: 'Team',
        id: DESIGN_TEAM_ID,
        key: 'DES',
        name: 'Design',
        workflowStates: [],
      },
    ],
  })
}

/**
 * Three cards: two in the first column, one in the second.
 *
 * Named rather than written inline, because the mutation fixtures echo them
 * back. `issueRow` derives a priority from the seed, so an echo assembled from
 * a bare `issueRow(1)` would quietly change Alpha's priority as well as its
 * state -- and would then fail a sort assertion for a reason that has nothing
 * to do with the sort.
 */
const ALPHA = issueRow(1, { title: 'Alpha', workflowStateId: ICEBOX, priority: 3 })
const BRAVO = issueRow(2, {
  title: 'Bravo',
  workflowStateId: BUILDING,
  priority: 1,
  assigneeId: MEMBER_ID,
})
const CHARLIE = issueRow(3, { title: 'Charlie', workflowStateId: ICEBOX, priority: 0 })

/**
 * One page, in the order the server would return it.
 *
 * The default sort is priority, which the server orders by
 * `NULLIF(priority, 0)` ascending: Bravo (1), Alpha (3), then Charlie (0 --
 * untriaged, and so last). Written in that order because nothing re-sorts it
 * here any more: the board groups these into columns and keeps the order they
 * arrived in.
 */
const CARDS: readonly IssueRowFields[] = [BRAVO, ALPHA, CHARLIE]

/** A successful `BoardIssueMove`: the card, echoed back with its new state. */
function moved(issue: IssueRowFields): BoardIssueMoveMutation {
  return {
    issueUpdate: { __typename: 'IssueUpdatePayload', issue, errors: [] },
  }
}

/** A refused one. `issue` is null exactly when `errors` is not empty. */
function moveRejected(
  ...errors: readonly IssueValidationError[]
): BoardIssueMoveMutation {
  return {
    issueUpdate: {
      __typename: 'IssueUpdatePayload',
      issue: null,
      errors: [...errors],
    },
  }
}

interface OpenBoardOptions {
  search?: string
  issues?: readonly IssueRowFields[]
}

/**
 * The board, with the workspace lookups and the first page answered.
 *
 * The context is answered first because the board cannot ask for issues until
 * it has a team: the filter carries a `UUID!`, so `useBoardIssues` skips the
 * query rather than sending a placeholder.
 *
 * `BoardLabels` and `TeamCycles` -- the two picker sources the workspace
 * context does not already hold -- are deliberately left unanswered. No test
 * here is about the pickers' options, and leaving them pending keeps "how many
 * requests were sent" a question about the board's own query.
 */
async function openBoard({ search = '', issues = CARDS }: OpenBoardOptions = {}) {
  const view = renderApp({ initialPath: `/${WORKSPACE_SLUG}/board${search}` })

  await view.link.resolve('IssueWorkspaceContext', { data: boardContext() })
  await view.link.resolve('BoardIssues', { data: issueListData(issues) })

  return view
}

/** Every column, in the order the board drew them. */
function columnNames(): string[] {
  return within(main())
    .getAllByRole('region')
    .map((region) => region.getAttribute('aria-label') ?? nameOf(region))
}

/** A region labelled by an element rather than by a string. */
function nameOf(region: HTMLElement): string {
  const id = region.getAttribute('aria-labelledby')

  return id === null ? '' : (document.getElementById(id)?.textContent ?? '')
}

function column(name: string): HTMLElement {
  return within(main()).getByRole('region', { name })
}

/** The card titles in one column, in order. */
function cards(name: string): string[] {
  return within(column(name))
    .queryAllByRole('link')
    .map((card) => card.textContent ?? '')
}

describe('board columns', () => {
  it('draws one column per workflow state, in the team’s own order and words', async () => {
    await openBoard()

    // The team's own names, in `position` order -- not the order the server
    // listed them in, and not a category name.
    expect(columnNames()).toEqual(['Icebox', 'Building', 'Shipped'])

    // The names this feature must never invent.
    expect(screen.queryByText('Todo')).toBeNull()
    expect(screen.queryByText('Doing')).toBeNull()
    expect(screen.queryByText('Done')).toBeNull()
  })

  it('puts each card in its state’s column and states a count it can stand behind', async () => {
    await openBoard()

    expect(cards('Icebox')).toEqual(['Alpha', 'Charlie'])
    expect(cards('Building')).toEqual(['Bravo'])
    expect(cards('Shipped')).toEqual([])

    // Every matching issue is loaded, so the count is what it looks like and
    // an empty column is empty rather than possibly-empty. Neither sentence
    // hedges, because neither has anything left to hedge about.
    expect(column('Icebox')).toHaveTextContent('2')
    expect(column('Icebox')).not.toHaveTextContent('loaded')
    expect(column('Shipped')).toHaveTextContent('Nothing here.')
    expect(main()).not.toHaveTextContent('There may be more on later pages')
  })

  it('qualifies the counts, and only then, while a page is outstanding', async () => {
    const view = renderApp({ initialPath: `/${WORKSPACE_SLUG}/board` })

    await view.link.resolve('IssueWorkspaceContext', { data: boardContext() })
    await view.link.resolve('BoardIssues', {
      data: issueListData(CARDS, { hasNextPage: true, endCursor: 'c1', totalCount: 9 }),
    })

    // Filtering on the server does not make a keyset-paginated list complete,
    // so this is the caveat that survives -- written around `totalCount`,
    // which is a fact rather than a count of what happened to be fetched.
    expect(main()).toHaveTextContent('Showing 3 of 9 issues')
    expect(column('Icebox')).toHaveTextContent('2 loaded')
    expect(column('Shipped')).toHaveTextContent('There may be more on later pages')
  })
})

describe('what the board asks the server for', () => {
  it('sends the team, and no filter for a control nobody touched', async () => {
    const view = renderApp({ initialPath: `/${WORKSPACE_SLUG}/board` })

    await view.link.resolve('IssueWorkspaceContext', { data: boardContext() })

    // The whole variables object, because the claim is about what is NOT in
    // it: an `assigneeId: null` here would ask for the unassigned issues, and
    // the board would render a plausible answer to a question nobody asked.
    await expect(view.link.waitForRequest('BoardIssues')).resolves.toEqual({
      workspaceSlug: WORKSPACE_SLUG,
      filter: { teamId: TEAM_ID },
      orderBy: { field: 'PRIORITY', direction: 'ASC' },
      after: null,
    })
  })

  it('sends the filters and the sort the address arrived with', async () => {
    const view = renderApp({
      initialPath: `/${WORKSPACE_SLUG}/board?assignee=none&priority=1&sort=due`,
    })

    await view.link.resolve('IssueWorkspaceContext', { data: boardContext() })

    // "Unassigned" is the one filter value that IS a null, and the only one.
    await expect(view.link.waitForRequest('BoardIssues')).resolves.toEqual({
      workspaceSlug: WORKSPACE_SLUG,
      filter: { teamId: TEAM_ID, assigneeId: null, priority: 1 },
      orderBy: { field: 'DUE_DATE', direction: 'ASC' },
      after: null,
    })
  })

  it('asks again when a filter changes, rather than sifting what it has', async () => {
    const { link, user } = await openBoard()

    expect(link.countOf('BoardIssues')).toBe(1)

    await user.selectOptions(screen.getByLabelText('Priority'), '1')

    await expect(link.waitForRequest('BoardIssues')).resolves.toMatchObject({
      filter: { teamId: TEAM_ID, priority: 1 },
    })
  })
})

describe('moving a card', () => {
  it('sends issueUpdate, applies it optimistically, and rolls back on refusal', async () => {
    const { link, user } = await openBoard()

    await user.click(screen.getByRole('button', { name: 'Move ENG-1' }))
    await user.click(screen.getByRole('menuitem', { name: 'Building' }))

    // The patch names one field. Everything else about the issue is left
    // alone, which is what makes `issueUpdate` safe to send from a board.
    expect(await link.waitForRequest('BoardIssueMove')).toEqual({
      id: issueId(1),
      input: { workspaceSlug: WORKSPACE_SLUG, workflowStateId: BUILDING },
    })

    // Optimistic: the card is in its new column before the server has
    // answered. Asserting after the response would pass either way.
    expect(cards('Building')).toEqual(['Bravo', 'Alpha'])
    expect(cards('Icebox')).toEqual(['Charlie'])

    await link.resolve('BoardIssueMove', {
      data: moveRejected(
        validationError('workflowStateId', 'NOT_FOUND', 'Unknown workflow state.'),
      ),
    })

    // Apollo drops the optimistic layer when the real result lands, so the
    // card returns with no rollback code of our own -- and the refusal is
    // reported rather than swallowed.
    expect(cards('Icebox')).toEqual(['Alpha', 'Charlie'])
    expect(cards('Building')).toEqual(['Bravo'])
    expect(within(main()).getByRole('alert')).toHaveTextContent(
      'ENG-1 could not be moved to Building. Unknown workflow state.',
    )
  })

  it('moves a card one column over from the keyboard, and announces it', async () => {
    const { link, user } = await openBoard()

    // Focused rather than clicked: the card's title is a link to the issue,
    // so clicking it would navigate. This is the state a keyboard user
    // reaches by tabbing.
    within(column('Icebox')).getByRole('link', { name: 'Alpha' }).focus()
    await user.keyboard('{Alt>}{ArrowRight}{/Alt}')

    expect(await link.waitForRequest('BoardIssueMove')).toEqual({
      id: issueId(1),
      input: { workspaceSlug: WORKSPACE_SLUG, workflowStateId: BUILDING },
    })

    // Exactly what the server echoes for a patch: the card it was sent, with
    // the one field changed.
    await link.resolve('BoardIssueMove', {
      data: moved({ ...ALPHA, workflowStateId: BUILDING }),
    })

    expect(cards('Building')).toEqual(['Bravo', 'Alpha'])

    // A move a mouse user sees happen has to be said out loud for everyone
    // else. `role="status"`, so it does not interrupt.
    expect(within(main()).getByRole('status')).toHaveTextContent(
      'ENG-1 moved to Building',
    )
  })

  it('keeps the named menu, and drops the arrows, when columns are not states', async () => {
    const { link, user } = await openBoard({ search: '?group=priority' })

    // Alpha is priority 3, so it sits in the Medium column now.
    within(column('Medium')).getByRole('link', { name: 'Alpha' }).focus()
    await user.keyboard('{Alt>}{ArrowRight}{/Alt}')
    await link.idle()

    // The arrows are spatial: "one column to the right" cannot mean a status
    // when the columns are priorities.
    expect(link.countOf('BoardIssueMove')).toBe(0)

    // The menu names its destination, so it still applies -- looking at the
    // work by priority must not take away the board's one write.
    await user.click(screen.getByRole('button', { name: 'Move ENG-1' }))
    await user.click(screen.getByRole('menuitem', { name: 'Building' }))

    expect(await link.waitForRequest('BoardIssueMove')).toEqual({
      id: issueId(1),
      input: { workspaceSlug: WORKSPACE_SLUG, workflowStateId: BUILDING },
    })
  })

  it('does nothing at the end of the board rather than wrapping around', async () => {
    const { link, user } = await openBoard()

    within(column('Icebox')).getByRole('link', { name: 'Alpha' }).focus()
    await user.keyboard('{Alt>}{ArrowLeft}{/Alt}')

    // Icebox is the first column. A card that reappeared at the far end is
    // the one movement a keyboard user cannot follow.
    await link.idle()
    expect(link.countOf('BoardIssueMove')).toBe(0)
    expect(cards('Icebox')).toEqual(['Alpha', 'Charlie'])
  })
})

describe('the view in the URL', () => {
  it('groups what the address asked for, over what the server sent back', async () => {
    // The server answered the priority filter, so the page is Bravo alone.
    // Grouping is the half of the view that is still this screen's job.
    await openBoard({ search: '?team=ENG&group=priority&priority=1', issues: [BRAVO] })

    expect(columnNames()).toEqual(['Urgent', 'High', 'Medium', 'Low', 'No priority'])
    expect(cards('Urgent')).toEqual(['Bravo'])
    expect(cards('Medium')).toEqual([])

    // And the controls show what the URL said, so the view is editable from
    // where it landed rather than only from the default.
    expect(screen.getByLabelText('Group by')).toHaveValue('priority')
    expect(screen.getByLabelText('Priority')).toHaveValue('1')
  })

  it('writes a control change back into the address', async () => {
    const { currentSearch, link, user } = await openBoard()

    await user.selectOptions(screen.getByLabelText('Group by'), 'assignee')

    // The claim: the URL *is* the state. Without this the board would still
    // regroup on screen and would still lose the view on a refresh -- which
    // is a regression no rendering assertion can see.
    expect(currentSearch()).toContain('group=assignee')

    expect(columnNames()).toEqual(['Ada Lovelace', 'Unassigned'])
    expect(cards('Ada Lovelace')).toEqual(['Bravo'])

    // Grouping is not a server argument and must not become a request: every
    // grouping of a board is the same cards in different columns.
    expect(link.countOf('BoardIssues')).toBe(1)

    // Sorting is a separate parameter, and the default is omitted from the
    // URL rather than written out.
    expect(currentSearch()).not.toContain('sort=')
  })

  it('drops a filter from the address when it is cleared, and asks again without it', async () => {
    const { currentSearch, link, user } = await openBoard({
      search: '?priority=1',
      issues: [BRAVO],
    })

    await user.click(screen.getByRole('button', { name: 'Clear filters' }))

    expect(currentSearch()).not.toContain('priority')

    // A different filter is a different list, so the unfiltered board is a
    // fresh request rather than a re-render of what was already loaded.
    await expect(link.waitForRequest('BoardIssues')).resolves.toMatchObject({
      filter: { teamId: TEAM_ID },
    })
    await link.resolve('BoardIssues', { data: issueListData(CARDS) })

    expect(cards('Icebox')).toEqual(['Alpha', 'Charlie'])
  })
})
