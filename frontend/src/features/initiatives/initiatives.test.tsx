import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import {
  expectEveryButtonNamed,
  expectHeadingLevelsUnbroken,
  expectNoDuplicateButtonNames,
  expectOneFirstLevelHeading,
} from '../../test/a11y'
import { main, renderApp } from '../../test/render'
import {
  MEMBER_ID,
  PROJECT_ID,
  WORKSPACE_SLUG,
  workspaceContextData,
} from '../../test/factories'
import type { InitiativeFieldsFragment } from '../../generated/operations'
import { buildInitiativeTree, parentCandidates } from './lib/initiatives'

/**
 * The initiatives screen, against the real router, cache and a controlled
 * network.
 *
 * Two claims matter most here and neither is about a happy path.
 *
 * The first is that `health` is not invented. `Initiative.health` is nullable
 * and null means "nobody has posted an update", which migration 022 is
 * explicit about. A screen that rendered null as "On track" would be telling
 * a reader that somebody had looked at a thing nobody had looked at.
 *
 * The second is that the tree does not lie about nesting. The hierarchy is
 * reconstructed from `parentInitiativeId` across the rows in hand, so a child
 * whose parent is on a later page has to be drawn somewhere -- and drawing it
 * as a top-level initiative without saying so would misstate the structure.
 */

const INITIATIVES_PATH = `/${WORKSPACE_SLUG}/initiatives`

const PARENT_ID = '00000000-0000-4000-8000-0000000dd001'
const CHILD_ID = '00000000-0000-4000-8000-0000000dd002'
const ORPHAN_ID = '00000000-0000-4000-8000-0000000dd003'
const OFFPAGE_PARENT_ID = '00000000-0000-4000-8000-0000000dd0ff'

function initiative(
  overrides: Partial<InitiativeFieldsFragment> = {},
): InitiativeFieldsFragment {
  return {
    __typename: 'Initiative',
    id: PARENT_ID,
    name: 'Q3 launch',
    description: null,
    status: 'ACTIVE',
    health: null,
    targetDate: null,
    ownerId: null,
    parentInitiativeId: null,
    projectIds: [],
    childInitiativeIds: [],
    updatedAt: '2026-01-15T12:00:00.000Z',
    ...overrides,
  }
}

function listData(
  nodes: readonly InitiativeFieldsFragment[],
  { hasNextPage = false, endCursor = null }: { hasNextPage?: boolean; endCursor?: string | null } = {},
) {
  return {
    initiatives: {
      __typename: 'InitiativeConnection' as const,
      nodes: [...nodes],
      pageInfo: { __typename: 'PageInfo' as const, hasNextPage, endCursor },
    },
  }
}

function detailData(node: InitiativeFieldsFragment) {
  return {
    initiative: {
      ...node,
      createdAt: '2026-01-01T12:00:00.000Z',
      updates: [],
    },
  }
}

async function openInitiatives(
  nodes: readonly InitiativeFieldsFragment[],
  options: { hasNextPage?: boolean; endCursor?: string | null } = {},
) {
  const app = renderApp({ initialPath: INITIATIVES_PATH })

  await app.link.resolve('IssueWorkspaceContext', { data: workspaceContextData() })
  await app.link.resolve('InitiativeList', { data: listData(nodes, options) })

  const first = nodes[0]

  if (first !== undefined) {
    // The panel opens on the first row, which asks for its updates.
    await app.link.resolve('InitiativeDetail', { data: detailData(first) })
  }

  return app
}

describe('the initiative list', () => {
  it('says nobody has reported rather than calling an unreported initiative on track', async () => {
    await openInitiatives([initiative({ health: null })])

    /*
      The optimistic default is the tempting one and it is a claim about a
      thing nobody has looked at. `initiativeUpdatePost` is the only way to
      set health, so null genuinely means no update exists.
    */
    const list = within(main()).getByRole('list', { name: 'Initiatives' })

    expect(within(list).getByText('No update yet')).toBeInTheDocument()

    // Scoped to the list: "On track" is also an option in the panel's health
    // picker, which is a control and not a claim about this initiative.
    expect(within(list).queryByText('On track')).not.toBeInTheDocument()
  })

  it('draws a child under its parent', async () => {
    await openInitiatives([
      initiative({ id: PARENT_ID, name: 'Q3 launch', childInitiativeIds: [CHILD_ID] }),
      initiative({ id: CHILD_ID, name: 'Billing rewrite', parentInitiativeId: PARENT_ID }),
    ])

    const rows = within(main()).getAllByRole('listitem')

    // Depth-first: the child follows its parent rather than sitting wherever
    // the server's newest-first ordering put it.
    expect(rows[0]).toHaveTextContent('Q3 launch')
    expect(rows[1]).toHaveTextContent('Billing rewrite')
  })

  it('says so when a row names a parent it is not drawn under', async () => {
    await openInitiatives(
      [initiative({ id: ORPHAN_ID, name: 'Docs refresh', parentInitiativeId: OFFPAGE_PARENT_ID })],
      { hasNextPage: true, endCursor: 'cursor-1' },
    )

    /*
      The row has to be drawn at the top level -- there is nowhere else to put
      it -- and a silent promotion would tell the reader this is a top-level
      initiative when it is not.
    */
    expect(
      within(main()).getByText('Nested under an initiative not shown here'),
    ).toBeInTheDocument()
  })

  it('offers a real next page rather than a button that does nothing', async () => {
    const app = await openInitiatives([initiative()], {
      hasNextPage: true,
      endCursor: 'cursor-1',
    })

    await app.user.click(screen.getByRole('button', { name: 'Load more' }))

    /*
      The cursor, not an offset. `fetchMore` only works because
      `src/lib/graphql/cache.ts` carries a field policy for `initiatives`;
      without one Apollo writes page two under a key no mounted query watches
      and the button silently changes nothing.
    */
    await expect(app.link.waitForRequest('InitiativeList')).resolves.toEqual({
      workspaceSlug: WORKSPACE_SLUG,
      after: 'cursor-1',
    })
  })

  it('says there are none rather than showing an empty list', async () => {
    await openInitiatives([])

    expect(within(main()).getByText('No initiatives yet')).toBeInTheDocument()
  })

  it('offers a retry when the list could not be loaded', async () => {
    const app = renderApp({ initialPath: INITIATIVES_PATH })

    await app.link.resolve('IssueWorkspaceContext', { data: workspaceContextData() })
    await app.link.fail('InitiativeList', new Error('Network unreachable'))

    const alert = await screen.findByRole('alert')

    expect(alert).toHaveTextContent('Could not load initiatives')

    await app.user.click(within(alert).getByRole('button', { name: 'Try again' }))

    await expect(app.link.waitForRequest('InitiativeList')).resolves.toEqual({
      workspaceSlug: WORKSPACE_SLUG,
      after: null,
    })
  })
})

describe('the initiative panel', () => {
  it('names the projects it can and says so about the ones it cannot', async () => {
    const unknownProject = '00000000-0000-4000-8000-0000000dd0aa'

    await openInitiatives([
      initiative({ projectIds: [PROJECT_ID, unknownProject] }),
    ])

    // `Initiative.projectIds` is raw UUIDs and the workspace context loads at
    // most 50 projects, so one of these has no name available.
    expect(await screen.findByText('Platform')).toBeInTheDocument()

    /*
      The unresolvable one is SHOWN. Dropping it would report an initiative of
      two projects as one of one -- a false statement about what the
      initiative contains, made by omission.
    */
    expect(screen.getByText('A project this screen cannot name')).toBeInTheDocument()
  })

  it('posts an update with the health and the sentence together', async () => {
    const app = await openInitiatives([initiative()])

    await app.user.selectOptions(await screen.findByLabelText('Health'), 'AT_RISK')
    await app.user.type(screen.getByLabelText('What changed'), 'Vendor slipped a week.')
    await app.user.click(screen.getByRole('button', { name: 'Post update' }))

    /*
      Health is not a field on either initiative mutation, by design: the only
      way to set it is to say why at the same time.
    */
    await expect(app.link.waitForRequest('InitiativeUpdatePost')).resolves.toEqual({
      input: {
        workspaceSlug: WORKSPACE_SLUG,
        initiativeId: PARENT_ID,
        health: 'AT_RISK',
        body: 'Vendor slipped a week.',
      },
    })
  })

  it('sends only what the editor changed, because the input is a patch', async () => {
    const app = await openInitiatives([
      initiative({ description: 'The whole of Q3.', ownerId: MEMBER_ID, targetDate: '2026-09-30' }),
    ])

    await app.user.click(screen.getByRole('button', { name: 'Actions on Q3 launch' }))
    await app.user.click(screen.getByRole('menuitem', { name: 'Edit initiative...' }))

    const name = await screen.findByLabelText('Initiative name')

    await app.user.clear(name)
    await app.user.type(name, 'Q3 launch, revised')
    await app.user.click(screen.getByRole('button', { name: 'Save changes' }))

    /*
      `InitiativeUpdateInput` leaves an absent field alone -- it is a patch,
      unlike `IssueTemplateUpdateInput`. The form still sends every field it
      rendered, which is what keeps the description, owner and date it
      displayed from being cleared by a rename.
    */
    await expect(app.link.waitForRequest('InitiativeUpdate')).resolves.toEqual({
      input: {
        workspaceSlug: WORKSPACE_SLUG,
        id: PARENT_ID,
        name: 'Q3 launch, revised',
        description: 'The whole of Q3.',
        status: 'ACTIVE',
        targetDate: '2026-09-30',
        ownerId: MEMBER_ID,
      },
    })
  })
})

describe('the tree the list is drawn from', () => {
  it('does not hang on a parent chain that loops back on itself', () => {
    /*
      The server refuses a cycle -- `initiativeSetParent` is where that rule
      lives -- and a client that trusted it and then walked the chain would
      hang the tab rather than report anything. This is the "what if the
      backstop is not there" case, which is the only kind of input that can
      show the walk terminates.
    */
    const rows = buildInitiativeTree([
      initiative({ id: PARENT_ID, name: 'A', parentInitiativeId: CHILD_ID }),
      initiative({ id: CHILD_ID, name: 'B', parentInitiativeId: PARENT_ID }),
    ])

    expect(rows.map((row) => row.initiative.name).sort()).toEqual(['A', 'B'])
  })

  it('does not offer an initiative its own descendant as a parent', () => {
    const candidates = parentCandidates(PARENT_ID, [
      initiative({ id: PARENT_ID, name: 'A' }),
      initiative({ id: CHILD_ID, name: 'B', parentInitiativeId: PARENT_ID }),
      initiative({ id: ORPHAN_ID, name: 'C' }),
    ])

    // Nesting A under B would be a cycle. The server refuses it; offering it
    // in a picker invites a refusal the user could not have predicted.
    expect(candidates.map((entry) => entry.name)).toEqual(['C'])
  })
})

describe('the initiatives screen is navigable without a mouse', () => {
  it('names every button, once', async () => {
    await openInitiatives([
      initiative({ id: PARENT_ID, name: 'Q3 launch', projectIds: [PROJECT_ID] }),
      initiative({ id: CHILD_ID, name: 'Billing rewrite', parentInitiativeId: PARENT_ID }),
    ])

    expectEveryButtonNamed()
    expectNoDuplicateButtonNames()
  })

  it('has one first-level heading and an unbroken outline', async () => {
    await openInitiatives([initiative()])

    expectOneFirstLevelHeading('Initiatives')
    expectHeadingLevelsUnbroken()
  })

  it('labels every control the composer draws', async () => {
    const app = await openInitiatives([initiative()])

    await app.user.click(screen.getByRole('button', { name: 'New initiative' }))

    expect(await screen.findByRole('textbox', { name: 'Initiative name' })).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Description' })).toBeInTheDocument()
    expect(screen.getByRole('combobox', { name: 'Status' })).toBeInTheDocument()
    expect(screen.getByRole('combobox', { name: 'Owner' })).toBeInTheDocument()
  })
})
