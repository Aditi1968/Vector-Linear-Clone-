import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { RelationsPanel } from './index'
import type { IssueRelationType } from './api'
import {
  ISSUE_ID,
  relation,
  relationCreated,
  relationsData,
  renderPanel,
  searchData,
  summary,
  uuid,
  WORKSPACE_SLUG,
} from './testHarness'

/**
 * Relations between issues.
 *
 * Two things are worth pinning. First, the type round-trip: the value the
 * picker sends is the value the server stores and the value the panel groups
 * by, with no inversion anywhere -- `BLOCKS` and `BLOCKED_BY` are two ends of
 * one edge, and a client that flipped them would show the opposite of the
 * truth on the other issue's screen. Second, that a created relation appears
 * without a refetch, because the payload carries only the relation and the
 * connection it belongs in is a cache field nothing in the response mentions.
 */

async function mount(nodes: readonly ReturnType<typeof relation>[] = []) {
  const view = renderPanel(
    <RelationsPanel workspaceSlug={WORKSPACE_SLUG} issueId={ISSUE_ID} />,
  )

  await view.link.resolve('IssueRelations', { data: relationsData(nodes) })

  return view
}

function groupRows(label: string): string[] {
  return within(screen.getByRole('list', { name: label }))
    .getAllByRole('listitem')
    .map((row) => row.textContent ?? '')
}

/** Open the picker, choose a type, search, and take the first hit. */
async function link(
  view: Awaited<ReturnType<typeof mount>>,
  type: IssueRelationType,
  hit: { id: string; identifier: string; title: string },
) {
  await view.user.click(screen.getByRole('button', { name: 'Add' }))
  await view.user.selectOptions(
    screen.getByRole('combobox', { name: 'This issue is' }),
    type,
  )
  await view.user.type(screen.getByRole('searchbox', { name: 'Search issues' }), 'auth')

  // The search is debounced; `waitForRequest` polls until it goes out.
  await view.link.resolve('IssueSearch', { data: searchData([hit]) })
  await view.user.click(screen.getByRole('button', { name: `${hit.identifier} ${hit.title}` }))
}

describe('relations on an issue', () => {
  it('says there are none rather than showing empty groups', async () => {
    await mount()

    expect(screen.getByText('No relations')).toBeInTheDocument()
  })

  it('groups by type and names each group', async () => {
    await mount([
      relation(10, 'BLOCKED_BY', summary(20, { title: 'Ship the schema' })),
      relation(11, 'RELATED', summary(21, { title: 'Rename the field' })),
    ])

    expect(groupRows('Blocked by')[0]).toContain('Ship the schema')
    expect(groupRows('Related to')[0]).toContain('Rename the field')
  })

  it('says it is blocked, in words, only for blockers that are still open', async () => {
    await mount([
      relation(10, 'BLOCKED_BY', summary(20)),
      relation(11, 'BLOCKED_BY', summary(21, { completedAt: '2026-01-01T00:00:00Z' })),
      relation(12, 'BLOCKS', summary(22)),
    ])

    // Two `BLOCKED_BY` rows, one of them finished. An issue blocked by
    // something already done is not blocked.
    expect(screen.getByText('Blocked by 1 unfinished issue')).toBeInTheDocument()
  })

  it('does not claim to be blocked when nothing blocks it', async () => {
    await mount([relation(12, 'BLOCKS', summary(22))])

    expect(screen.queryByText(/^Blocked by \d/)).toBeNull()
  })

  it('sends the type the picker chose, unchanged', async () => {
    const view = await mount()

    await link(view, 'BLOCKS', {
      id: uuid(30),
      identifier: 'VEC-30',
      title: 'Migrate the table',
    })

    expect(await view.link.waitForRequest('IssueRelationCreate')).toEqual({
      input: {
        workspaceSlug: WORKSPACE_SLUG,
        // The issue on screen is always the source; the panel never relates
        // two issues neither of which it is showing.
        sourceIssueId: ISSUE_ID,
        targetIssueId: uuid(30),
        type: 'BLOCKS',
      },
    })
  })

  it('shows a created relation in its own group without refetching', async () => {
    const view = await mount([relation(10, 'RELATED', summary(20))])

    await link(view, 'BLOCKED_BY', {
      id: uuid(30),
      identifier: 'VEC-30',
      title: 'Migrate the table',
    })

    await view.link.resolve('IssueRelationCreate', {
      data: relationCreated(
        relation(31, 'BLOCKED_BY', summary(30, { title: 'Migrate the table' })),
      ),
    })

    expect(groupRows('Blocked by')[0]).toContain('Migrate the table')
    // The pre-existing relation is still there: the update inserted, it did
    // not replace.
    expect(groupRows('Related to')).toHaveLength(1)
    expect(view.link.countOf('IssueRelations')).toBe(1)
    // A new blocker is a new reason the issue cannot move.
    expect(screen.getByText('Blocked by 1 unfinished issue')).toBeInTheDocument()
  })

  it('removes a relation from the panel it was removed from', async () => {
    const view = await mount([
      relation(10, 'RELATED', summary(20, { title: 'Rename the field' })),
      relation(11, 'RELATED', summary(21, { title: 'Ship the schema' })),
    ])

    await view.user.click(
      screen.getByRole('button', { name: 'Remove related to Rename the field' }),
    )

    expect(await view.link.waitForRequest('IssueRelationDelete')).toEqual({
      input: { workspaceSlug: WORKSPACE_SLUG, id: uuid(10) },
    })

    await view.link.resolve('IssueRelationDelete', {
      data: {
        issueRelationDelete: {
          __typename: 'IssueRelationDeletePayload' as const,
          deletedRelationId: uuid(10),
          errors: [],
        },
      },
    })

    expect(groupRows('Related to')).toHaveLength(1)
    expect(groupRows('Related to')[0]).toContain('Ship the schema')
  })

  it('does not offer this issue or one already linked as a target', async () => {
    const view = await mount([
      relation(10, 'RELATED', summary(20, { title: 'Already linked' })),
    ])

    await view.user.click(screen.getByRole('button', { name: 'Add' }))
    await view.user.type(
      screen.getByRole('searchbox', { name: 'Search issues' }),
      'issue',
    )
    await view.link.resolve('IssueSearch', {
      data: searchData([
        { id: ISSUE_ID, identifier: 'VEC-1', title: 'This issue' },
        { id: uuid(20), identifier: 'VEC-20', title: 'Already linked' },
        { id: uuid(30), identifier: 'VEC-30', title: 'Fair game' },
      ]),
    })

    const results = within(screen.getByRole('list', { name: 'Search results' }))
      .getAllByRole('listitem')
      .map((row) => row.textContent ?? '')

    // Both would be refused by the server. Not offering a choice that cannot
    // work beats explaining the refusal afterwards.
    expect(results).toHaveLength(1)
    expect(results[0]).toContain('Fair game')
  })
})
