import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { SubIssuesPanel } from './index'
import {
  ISSUE_ID,
  parentSet,
  renderPanel,
  searchData,
  subIssuesData,
  summary,
  uuid,
  WORKSPACE_SLUG,
} from './testHarness'

/**
 * An issue's parent and its sub-issues.
 *
 * The thing this file exists to catch is the direction. `issueSetParent`
 * takes the CHILD's id whichever control the user pressed, so "choose my
 * parent" and "adopt a sub-issue" are the same mutation with the two ids
 * swapped -- and getting it backwards succeeds, quietly building the opposite
 * hierarchy. Both directions are asserted on the variables that go out, not
 * only on what appears afterwards.
 */

async function mount(
  parent: ReturnType<typeof summary> | null = null,
  children: readonly ReturnType<typeof summary>[] = [],
) {
  const view = renderPanel(
    <SubIssuesPanel workspaceSlug={WORKSPACE_SLUG} issueId={ISSUE_ID} />,
  )

  await view.link.resolve('IssueSubIssues', { data: subIssuesData(parent, children) })

  return view
}

function childRows(): string[] {
  const list = screen.queryByRole('list', { name: 'Sub-issues' })

  return list === null
    ? []
    : within(list)
        .getAllByRole('listitem')
        .map((row) => row.textContent ?? '')
}

async function pick(
  view: Awaited<ReturnType<typeof mount>>,
  trigger: string,
  hit: { id: string; identifier: string; title: string },
) {
  await view.user.click(screen.getByRole('button', { name: trigger }))
  await view.user.type(screen.getByRole('searchbox', { name: 'Search issues' }), 'auth')
  await view.link.resolve('IssueSearch', { data: searchData([hit]) })
  await view.user.click(
    screen.getByRole('button', { name: `${hit.identifier} ${hit.title}` }),
  )
}

describe('sub-issues and the parent issue', () => {
  it('says there is no parent and no children rather than showing nothing', async () => {
    await mount()

    expect(screen.getByText('No parent issue')).toBeInTheDocument()
    expect(screen.getByText('No sub-issues')).toBeInTheDocument()
    expect(childRows()).toEqual([])
  })

  it('links to the parent and to every child', async () => {
    await mount(summary(2, { title: 'Epic: auth' }), [summary(3, { title: 'Login' })])

    expect(screen.getByRole('link', { name: 'Epic: auth' })).toHaveAttribute(
      'href',
      `/${WORKSPACE_SLUG}/issues/${uuid(2)}`,
    )
    expect(screen.getByRole('link', { name: 'Login' })).toHaveAttribute(
      'href',
      `/${WORKSPACE_SLUG}/issues/${uuid(3)}`,
    )
  })

  it('sets a parent with THIS issue as the subject', async () => {
    const view = await mount()

    await pick(view, 'Set parent', {
      id: uuid(2),
      identifier: 'VEC-2',
      title: 'Epic: auth',
    })

    // This issue is the child; the picked one becomes its parent.
    expect(await view.link.waitForRequest('IssueSetParent')).toEqual({
      input: { workspaceSlug: WORKSPACE_SLUG, issueId: ISSUE_ID, parentId: uuid(2) },
    })

    await view.link.resolve('IssueSetParent', {
      data: parentSet(summary(1), summary(2, { title: 'Epic: auth' })),
    })

    // No hand-written update for this direction: the payload describes this
    // issue, so Apollo's normalised write is the whole of it.
    expect(screen.getByRole('link', { name: 'Epic: auth' })).toBeInTheDocument()
    expect(view.link.countOf('IssueSubIssues')).toBe(1)
  })

  it('adds a sub-issue with the OTHER issue as the subject', async () => {
    const view = await mount(null, [summary(3, { title: 'Login' })])

    await pick(view, 'Add', { id: uuid(4), identifier: 'VEC-4', title: 'Logout' })

    // The ids are the other way round from `Set parent`, and this is the
    // assertion that catches them being swapped.
    expect(await view.link.waitForRequest('IssueSetParent')).toEqual({
      input: { workspaceSlug: WORKSPACE_SLUG, issueId: uuid(4), parentId: ISSUE_ID },
    })

    await view.link.resolve('IssueSetParent', {
      data: parentSet(summary(4, { title: 'Logout' }), summary(1)),
    })

    // The payload describes somebody else, so this issue's `children` is a
    // cache field nothing in the response mentions -- and it still updated.
    expect(childRows()).toHaveLength(2)
    expect(childRows().join(' ')).toContain('Logout')
    expect(view.link.countOf('IssueSubIssues')).toBe(1)
  })

  it('inserts a sub-issue where the server sorts it, not at whichever end is handy', async () => {
    // `children` is ordered by the CHILD's createdAt DESC, and `summary(n)`
    // gets older as n grows, so seed 4 belongs between 3 and 5.
    const view = await mount(null, [summary(3), summary(5)])

    await pick(view, 'Add', { id: uuid(4), identifier: 'VEC-4', title: 'Issue 4' })
    await view.link.resolve('IssueSetParent', {
      data: parentSet(summary(4), summary(1)),
    })

    expect(childRows().map((row) => row.replace(/\D+/g, ''))).toEqual(['3', '4', '5'])
  })

  it('clears the parent without touching the sub-issues', async () => {
    const view = await mount(summary(2, { title: 'Epic: auth' }), [summary(3)])

    await view.user.click(
      screen.getByRole('button', { name: 'Remove Epic: auth as the parent issue' }),
    )

    expect(await view.link.waitForRequest('IssueClearParent')).toEqual({
      input: { workspaceSlug: WORKSPACE_SLUG, issueId: ISSUE_ID },
    })

    await view.link.resolve('IssueClearParent', {
      data: {
        issueClearParent: {
          __typename: 'IssueParentPayload' as const,
          issue: {
            __typename: 'Issue' as const,
            id: ISSUE_ID,
            title: 'Issue 1',
            completedAt: null,
            createdAt: summary(1).createdAt,
            parent: null,
          },
          errors: [],
        },
      },
    })

    expect(screen.getByText('No parent issue')).toBeInTheDocument()
    expect(childRows()).toHaveLength(1)
  })

  it('releases a sub-issue by clearing THAT issue parent', async () => {
    const view = await mount(null, [
      summary(3, { title: 'Login' }),
      summary(4, { title: 'Logout' }),
    ])

    await view.user.click(
      screen.getByRole('button', { name: 'Remove Login as a sub-issue' }),
    )

    // The child is the subject again, not this issue.
    expect(await view.link.waitForRequest('IssueClearParent')).toEqual({
      input: { workspaceSlug: WORKSPACE_SLUG, issueId: uuid(3) },
    })

    await view.link.resolve('IssueClearParent', {
      data: {
        issueClearParent: {
          __typename: 'IssueParentPayload' as const,
          issue: {
            __typename: 'Issue' as const,
            id: uuid(3),
            title: 'Login',
            completedAt: null,
            createdAt: summary(3).createdAt,
            parent: null,
          },
          errors: [],
        },
      },
    })

    expect(childRows()).toHaveLength(1)
    expect(childRows()[0]).toContain('Logout')
    expect(
      within(screen.getByRole('region', { name: /Sub-issues/ })).getByRole('status'),
    ).toHaveTextContent('Login is no longer a sub-issue')
  })

  it('does not offer this issue, its parent, or a current child as a pick', async () => {
    const view = await mount(summary(2, { title: 'Epic: auth' }), [
      summary(3, { title: 'Login' }),
    ])

    await view.user.click(screen.getByRole('button', { name: 'Add' }))
    await view.user.type(
      screen.getByRole('searchbox', { name: 'Search issues' }),
      'issue',
    )
    await view.link.resolve('IssueSearch', {
      data: searchData([
        { id: ISSUE_ID, identifier: 'VEC-1', title: 'This issue' },
        { id: uuid(2), identifier: 'VEC-2', title: 'Epic: auth' },
        { id: uuid(3), identifier: 'VEC-3', title: 'Login' },
        { id: uuid(9), identifier: 'VEC-9', title: 'Fair game' },
      ]),
    })

    const results = within(screen.getByRole('list', { name: 'Search results' }))
      .getAllByRole('listitem')
      .map((row) => row.textContent ?? '')

    expect(results).toHaveLength(1)
    expect(results[0]).toContain('Fair game')
  })
})
