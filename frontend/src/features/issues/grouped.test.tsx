import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import {
  DONE_STATE_ID,
  TODO_STATE_ID,
  issueListData,
  issueRow,
  workspaceContextData,
} from '../../test/factories'
import { main, renderApp } from '../../test/render'

/**
 * The grouped list mode, end to end.
 *
 * ../lib/grouping.test.ts pins the bucketing rules; this file pins the only
 * two things a render can add to that -- the accessible structure a grouped
 * list is supposed to have (a header, then a `List` carrying the same name,
 * so a screen reader hears "Todo, list, 2 items" and can skip the run) and
 * the fact that the flat list is what shows while the state lookup is still
 * in flight, rather than one anonymous bucket that re-splits a moment later.
 *
 * Kept out of ./list.test.tsx because that file's `markers()` helper asks for
 * *the* list with `queryByRole('list')`, which is the right query for a flat
 * list and throws on a grouped one.
 */

/** Turn the stored preference on before the shell mounts and reads it. */
function preferGrouped(): void {
  localStorage.setItem('vector.list.group', 'grouped')
}

const twoStates = issueListData([
  issueRow(1, { title: 'Alpha', workflowStateId: TODO_STATE_ID }),
  issueRow(2, { title: 'Bravo', workflowStateId: DONE_STATE_ID }),
  issueRow(3, { title: 'Charlie', workflowStateId: TODO_STATE_ID }),
])

describe('grouped issue list', () => {
  it('draws one run of rows per state, in lifecycle order', async () => {
    preferGrouped()

    const view = renderApp()

    await view.link.resolve('IssueWorkspaceContext', { data: workspaceContextData() })
    await view.link.resolve('IssueList', { data: twoStates })

    const lists = within(main()).getAllByRole('list')

    expect(lists.map((list) => list.getAttribute('aria-label'))).toEqual([
      'Todo',
      'Done',
    ])
    expect(within(lists[0] as HTMLElement).getAllByRole('link')).toHaveLength(2)
    expect(within(lists[1] as HTMLElement).getAllByRole('link')).toHaveLength(1)

    // The header names the run visibly; the list beside it announces the same
    // name and its own count, which is why the header is not a heading.
    expect(within(main()).getByText('Todo')).toBeInTheDocument()
    expect(within(main()).getByText('Done')).toBeInTheDocument()
  })

  it('stays flat while the workflow states are still unresolved', async () => {
    preferGrouped()

    const view = renderApp()

    // Deliberately no `IssueWorkspaceContext`: the rows are on screen and
    // their states are not, which is the window a half-built grouped view
    // would appear in.
    await view.link.resolve('IssueList', { data: twoStates })

    const lists = within(main()).getAllByRole('list')

    expect(lists).toHaveLength(1)
    expect(lists[0]).toHaveAttribute('aria-label', 'Issues')
    expect(within(lists[0] as HTMLElement).getAllByRole('link')).toHaveLength(3)
  })

  it('is flat by default, and the header offers the choice', async () => {
    const view = renderApp()

    await view.link.resolve('IssueWorkspaceContext', { data: workspaceContextData() })
    await view.link.resolve('IssueList', { data: twoStates })

    expect(within(main()).getAllByRole('list')).toHaveLength(1)

    // Both of the design's header controls, as radio groups: one tab stop
    // each, arrow keys to move, and the whole group announced with a position.
    expect(screen.getByRole('radiogroup', { name: 'Row density' })).toBeInTheDocument()
    expect(screen.getByRole('radiogroup', { name: 'Group issues' })).toBeInTheDocument()
  })

  it('regroups when the control is used, without refetching', async () => {
    const view = renderApp()

    await view.link.resolve('IssueWorkspaceContext', { data: workspaceContextData() })
    await view.link.resolve('IssueList', { data: twoStates })

    await view.user.click(screen.getByRole('radio', { name: 'Grouped' }))

    expect(
      within(main())
        .getAllByRole('list')
        .map((list) => list.getAttribute('aria-label')),
    ).toEqual(['Todo', 'Done'])
  })
})
