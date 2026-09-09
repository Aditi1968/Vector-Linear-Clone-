import { screen, within } from '@testing-library/react'
import type { UserEvent } from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import {
  CYCLE_ID,
  DONE_STATE_ID,
  FORMER_MEMBER_ID,
  issueArchived,
  issueDetail,
  issueDetailData,
  issueId,
  issueListData,
  issueProjectSet,
  issueRow,
  issueUpdated,
  issueUpdateRejected,
  MEMBER_ID,
  PROJECT_ID,
  teamCyclesData,
  validationError,
  workspaceContextData,
  WORKSPACE_SLUG,
} from '../../test/factories'
import { issueRows, main, renderApp } from '../../test/render'

/**
 * Editing an issue in the inspector.
 *
 * These are the tests that would catch a regression a user would actually
 * hit, which is a narrower set than "one per control":
 *
 *   - what goes out on the wire, because a patch that sent the wrong field
 *     name, or sent `undefined` where the user chose "nobody", silently does
 *     the wrong thing and still renders;
 *   - that the list and the inspector agree afterwards, which is the whole
 *     claim behind not writing a cache update for `issueUpdate`;
 *   - that a rejection lands on the control that caused it rather than in a
 *     banner, because that is the difference between a form a person can fix
 *     and one they can only retry.
 *
 * There is deliberately no test that each of the eight controls renders.
 */

const ALPHA_ID = issueId(1)
const detailPath = `/${WORKSPACE_SLUG}/issues/${ALPHA_ID}`

/** An issue open in the inspector, with the workspace lookups answered. */
async function openIssue(overrides = {}) {
  const view = renderApp({ initialPath: detailPath })

  await view.link.resolve('IssueWorkspaceContext', { data: workspaceContextData() })
  await view.link.resolve('IssueList', {
    data: issueListData([issueRow(1, { title: 'Alpha' })]),
  })
  await view.link.resolve('IssueDetail', {
    data: issueDetailData(issueDetail(1, { title: 'Alpha', ...overrides })),
  })

  return view
}

function inspector(): HTMLElement {
  return screen.getByRole('complementary', { name: 'Issue detail' })
}

function field(name: string): HTMLElement {
  return within(inspector()).getByRole('textbox', { name })
}

function picker(name: string): HTMLElement {
  return within(inspector()).getByRole('combobox', { name })
}

/** Type into a field and leave it, which is how this panel commits. */
async function retype(user: UserEvent, control: HTMLElement, value: string) {
  await user.clear(control)
  if (value !== '') {
    await user.type(control, value)
  }
  await user.tab()
}

describe('editing an issue', () => {
  it('sends the field that changed, and only that field', async () => {
    const { link, user } = await openIssue()

    await retype(user, field('Title'), 'Alpha renamed')

    const variables = await link.waitForRequest('IssueUpdate')

    // The id is an argument and the rest is the input, which is how the
    // schema declares it. A patch carrying the untouched fields would look
    // identical on screen and would clobber a concurrent edit to any of them.
    expect(variables).toEqual({
      id: ALPHA_ID,
      input: { workspaceSlug: WORKSPACE_SLUG, title: 'Alpha renamed' },
    })
  })

  it('does not send anything when the value is unchanged', async () => {
    const { link, user } = await openIssue()

    // Tabbing through a panel of eight fields must not fire eight mutations.
    await user.click(field('Title'))
    await user.tab()
    await user.click(field('Description'))
    await user.tab()

    await link.idle()
    expect(link.countOf('IssueUpdate')).toBe(0)
  })

  it('reverts on Escape without saving', async () => {
    const { link, user } = await openIssue()

    const title = field('Title')
    await user.click(title)
    await user.keyboard('{Backspace}{Backspace}')
    await user.keyboard('{Escape}')

    await link.idle()
    expect(link.countOf('IssueUpdate')).toBe(0)
    expect(field('Title')).toHaveValue('Alpha')
  })

  it('unassigns with an explicit null rather than by omitting the field', async () => {
    const { link, user } = await openIssue({ assigneeId: MEMBER_ID })

    await user.selectOptions(picker('Assignee'), '')

    const variables = await link.waitForRequest('IssueUpdate')

    // Omitting the key would leave the assignee in place: this mutation is a
    // patch, so absent and null mean different things.
    expect(variables).toEqual({
      id: ALPHA_ID,
      input: { workspaceSlug: WORKSPACE_SLUG, assigneeId: null },
    })
  })

  it('offers nobody who has left as an assignee, and still names them as the creator', async () => {
    const view = renderApp({ initialPath: detailPath })

    await view.link.resolve('IssueWorkspaceContext', {
      data: workspaceContextData({
        workspaceMembers: [
          {
            __typename: 'WorkspaceMember',
            userId: MEMBER_ID,
            name: 'Ada Lovelace',
            email: 'ada@example.com',
            removedAt: null,
          },
          {
            // `workspaceMembers` returns this row on purpose -- 026 stamps a
            // removed membership rather than deleting it -- so the two halves
            // of this test come from one response and one list.
            __typename: 'WorkspaceMember',
            userId: FORMER_MEMBER_ID,
            name: 'Alonzo Church',
            email: 'alonzo@example.com',
            removedAt: '2026-06-01T00:00:00.000Z',
          },
        ],
      }),
    })
    await view.link.resolve('IssueList', {
      data: issueListData([issueRow(1, { title: 'Alpha' })]),
    })
    await view.link.resolve('IssueDetail', {
      data: issueDetailData(
        issueDetail(1, { title: 'Alpha', creatorId: FORMER_MEMBER_ID }),
      ),
    })

    const assignee = picker('Assignee')

    expect(
      within(assignee).getByRole('option', { name: 'Ada Lovelace' }),
    ).toBeInTheDocument()
    // Assigning work to somebody who cannot open the issue is the bug this
    // guards: `find_membership` filters `removed_at IS NULL`, so they would
    // never see it.
    expect(
      within(assignee).queryByRole('option', { name: 'Alonzo Church' }),
    ).toBeNull()

    // ...and the same person still has a name in the log, which is why the
    // list is not filtered on the server. Dropping them would make "created
    // by somebody who left" render as "created by nobody".
    expect(within(inspector()).getByText('Alonzo Church')).toBeInTheDocument()
  })

  it('clears a due date the same way', async () => {
    const { link, user } = await openIssue({ dueDate: '2026-03-14' })

    await retype(user, within(inspector()).getByLabelText('Due date'), '')

    const variables = await link.waitForRequest('IssueUpdate')
    expect(variables).toEqual({
      id: ALPHA_ID,
      input: { workspaceSlug: WORKSPACE_SLUG, dueDate: null },
    })
  })

  it('routes project and cycle through their own mutations', async () => {
    const { link, user } = await openIssue()

    // `IssueUpdateInput` has neither field; the schema exposes these as
    // separate mutations, and the panel must not pretend otherwise.
    await link.resolve('TeamCycles', { data: teamCyclesData() })

    await user.selectOptions(picker('Project'), PROJECT_ID)
    expect(await link.waitForRequest('IssueSetProject')).toEqual({
      input: { workspaceSlug: WORKSPACE_SLUG, issueId: ALPHA_ID, projectId: PROJECT_ID },
    })
    await link.resolve('IssueSetProject', {
      data: issueProjectSet(
        issueDetail(1, {
          title: 'Alpha',
          project: { __typename: 'Project', id: PROJECT_ID, name: 'Platform' },
        }),
      ),
    })

    await user.selectOptions(picker('Cycle'), CYCLE_ID)
    expect(await link.waitForRequest('IssueSetCycle')).toEqual({
      input: { workspaceSlug: WORKSPACE_SLUG, issueId: ALPHA_ID, cycleId: CYCLE_ID },
    })
  })

  it('shows the new value in the list without a refetch', async () => {
    const { link, user } = await openIssue()

    await retype(user, field('Title'), 'Alpha renamed')
    await link.resolve('IssueUpdate', {
      data: issueUpdated(issueDetail(1, { title: 'Alpha renamed' })),
    })

    /*
      The coherence claim, and the reason `issueUpdate` needs no cache write:
      the mutation selects a superset of the row fragment, so Apollo
      normalises the result over the entity the cached list already points
      at. If the detail fragment ever stopped covering the row's fields this
      would still pass on the panel and fail here.
    */
    const [row] = issueRows()
    expect(row).toHaveAccessibleName(/Alpha renamed/)
    expect(link.countOf('IssueList')).toBe(1)
  })

  it('puts a rejection on the control that caused it', async () => {
    const { link, user } = await openIssue()

    await retype(user, field('Title'), 'x')
    await link.resolve('IssueUpdate', {
      data: issueUpdateRejected(
        validationError('title', 'TOO_LONG', 'Title must be at most 500 characters.'),
      ),
    })

    const title = field('Title')
    expect(title).toHaveAttribute('aria-invalid', 'true')
    // Described by the message, not merely next to it on screen.
    expect(title).toHaveAccessibleDescription(
      'Title must be at most 500 characters.',
    )

    // And not as a banner. A field-level rejection reported at the top of the
    // panel is the failure this whole grouping exists to prevent.
    expect(within(inspector()).queryByRole('alert')).toBeNull()
  })

  it('reports a transport failure once, where no field owns it', async () => {
    const { link, user } = await openIssue()

    await retype(user, field('Title'), 'Alpha renamed')
    await link.fail('IssueUpdate', new Error('Failed to fetch'))

    expect(within(inspector()).getByRole('alert')).toHaveTextContent('Failed to fetch')
    expect(field('Title')).not.toHaveAttribute('aria-invalid')
  })

  it('rolls the optimistic value back when the server rejects it', async () => {
    const { link, user } = await openIssue()

    await user.selectOptions(picker('Status'), DONE_STATE_ID)

    // Optimistically applied: the point of the optimistic response is that
    // this is true before the server has answered.
    expect(picker('Status')).toHaveValue(DONE_STATE_ID)

    await link.resolve('IssueUpdate', {
      data: issueUpdateRejected(
        validationError('workflowStateId', 'NOT_FOUND', 'Unknown workflow state.'),
      ),
    })

    // Apollo drops the optimistic layer when the real result lands, so the
    // previous value returns with no rollback code of our own.
    expect(picker('Status')).toHaveValue(
      workspaceContextData().teams[0]?.workflowStates[0]?.id ?? '',
    )
  })

  it('removes an archived issue from the list and closes the panel', async () => {
    const { link, user, currentPath } = await openIssue()

    await user.click(
      within(inspector()).getByRole('button', { name: /More actions/ }),
    )
    await user.click(screen.getByRole('menuitem', { name: 'Archive issue' }))

    expect(await link.waitForRequest('IssueArchive')).toEqual({
      workspaceSlug: WORKSPACE_SLUG,
      id: ALPHA_ID,
    })
    await link.resolve('IssueArchive', { data: issueArchived(ALPHA_ID) })

    /*
      The one mutation that genuinely needs a cache write: an archived issue
      is absent from every later query, so there is no entity left to
      normalise and the row would otherwise sit there until a reload.
    */
    expect(within(main()).queryByRole('list')).toBeNull()
    expect(currentPath()).toBe(`/${WORKSPACE_SLUG}/issues`)
    expect(link.countOf('IssueList')).toBe(1)
  })
})
