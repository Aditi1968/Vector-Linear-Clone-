import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { main, renderApp } from '../../test/render'
import { WORKSPACE_SLUG } from '../../test/factories'
import type { CycleFields } from './api'
import type {
  CycleIssuesQuery,
  CycleListQuery,
  CycleTeamsQuery,
  CycleUnscheduledIssuesQuery,
} from '../../generated/operations'

/**
 * The cycle screens, against the real router, cache and a controlled network.
 *
 * ## Why the fixtures are relative to `Date.now()` and the clock is not frozen
 *
 * "Current" is derived from `startsAt`/`endsAt` against now, so fixed dates
 * plus a real clock is a suite that starts failing on a particular Tuesday.
 * The obvious fix -- `vi.useFakeTimers()` -- is the wrong one here: the
 * controlled link settles React and Apollo through `setTimeout`, so a frozen
 * clock deadlocks every `resolve()` in the file. Offsets from `Date.now()`
 * give date-independence with no timer interference.
 *
 * The exact-boundary behaviour (a cycle ending at the instant the next
 * begins) needs a controlled instant and is pinned in ./lib/cycles.test.ts,
 * where it can be tested as the pure function it is.
 */

const DAY = 24 * 60 * 60 * 1000

/** An ISO instant `days` from now; negative is in the past. */
function daysFromNow(days: number): string {
  return new Date(Date.now() + days * DAY).toISOString()
}

const ENG = '00000000-0000-4000-8000-0000000000c1'
const DES = '00000000-0000-4000-8000-0000000000c2'
const CYCLE_ID = '00000000-0000-4000-8000-0000000000d2'

const teamsData: CycleTeamsQuery = {
  teams: [
    { __typename: 'Team', id: ENG, key: 'ENG', name: 'Engineering' },
    { __typename: 'Team', id: DES, key: 'DES', name: 'Design' },
  ],
}

function cycle(overrides: Partial<CycleFields> = {}): CycleFields {
  return {
    __typename: 'Cycle',
    id: CYCLE_ID,
    teamId: ENG,
    number: 12,
    name: null,
    startsAt: daysFromNow(-7),
    endsAt: daysFromNow(7),
    ...overrides,
  }
}

const previous = cycle({
  id: '00000000-0000-4000-8000-0000000000d1',
  number: 11,
  startsAt: daysFromNow(-21),
  endsAt: daysFromNow(-7),
})

const next = cycle({
  id: '00000000-0000-4000-8000-0000000000d3',
  number: 13,
  startsAt: daysFromNow(7),
  endsAt: daysFromNow(21),
})

function listData(cycles: readonly CycleFields[]): CycleListQuery {
  return { cycles: [...cycles] }
}

/** One page of the cycle's issues, as the server would answer the filter. */
function issuesData(
  nodes: CycleIssuesQuery['issues']['nodes'] = [],
  { hasNextPage = false, totalCount = nodes.length } = {},
): CycleIssuesQuery {
  return {
    issues: {
      __typename: 'IssueConnection',
      nodes: [...nodes],
      pageInfo: {
        __typename: 'PageInfo',
        hasNextPage,
        endCursor: hasNextPage ? 'cursor-1' : null,
      },
      totalCount,
    },
  }
}

function cycleIssue(title: string): CycleIssuesQuery['issues']['nodes'][number] {
  return {
    __typename: 'Issue',
    id: '00000000-0000-4000-8000-0000000000e1',
    identifier: 'ENG-1',
    title,
    completedAt: null,
  }
}

const noUnscheduled: CycleUnscheduledIssuesQuery = {
  issues: { __typename: 'IssueConnection', nodes: [] },
}

/** The section for one phase, found by the heading that names it. */
function phase(name: 'Current' | 'Upcoming' | 'Past'): HTMLElement {
  return within(main()).getByRole('region', { name })
}

describe('the cycle list', () => {
  it('cannot ask for cycles before it knows a team', async () => {
    const view = renderApp({ initialPath: `/${WORKSPACE_SLUG}/cycles` })

    await view.link.waitForRequest('CycleTeams')

    // `cycles(teamId:)` requires a team, so the list query is skipped rather
    // than sent with a placeholder -- a `UUID!` coerced from `''` would be a
    // top-level error reported as "something went wrong".
    expect(view.link.countOf('CycleList')).toBe(0)
  })

  it('asks for the first team once the teams arrive', async () => {
    const view = renderApp({ initialPath: `/${WORKSPACE_SLUG}/cycles` })

    await view.link.resolve('CycleTeams', { data: teamsData })

    await expect(view.link.waitForRequest('CycleList')).resolves.toEqual({
      workspaceSlug: WORKSPACE_SLUG,
      teamId: ENG,
    })
  })

  it('asks again for the team the picker is changed to', async () => {
    const view = renderApp({ initialPath: `/${WORKSPACE_SLUG}/cycles` })

    await view.link.resolve('CycleTeams', { data: teamsData })
    await view.link.resolve('CycleList', { data: listData([]) })

    await view.user.selectOptions(screen.getByLabelText('Team'), DES)

    // A different team is a different list, not a different page of one --
    // which is also why the cache keys `cycles` on `teamId` by default.
    await expect(view.link.waitForRequest('CycleList')).resolves.toEqual({
      workspaceSlug: WORKSPACE_SLUG,
      teamId: DES,
    })
  })

  it('says a team has no cycles rather than showing three empty headings', async () => {
    const view = renderApp({ initialPath: `/${WORKSPACE_SLUG}/cycles` })

    await view.link.resolve('CycleTeams', { data: teamsData })
    await view.link.resolve('CycleList', { data: listData([]) })

    expect(within(main()).getByText('No cycles for ENG')).toBeInTheDocument()
  })

  it('sorts one team run into current, upcoming and past', async () => {
    const view = renderApp({ initialPath: `/${WORKSPACE_SLUG}/cycles` })

    await view.link.resolve('CycleTeams', { data: teamsData })
    // Fed in `number` order, which is what the server returns and is neither
    // of the two orders the screen shows. The backend labels none of them --
    // there is no `current` field -- so every placement below is this
    // frontend's derivation being exercised end to end.
    await view.link.resolve('CycleList', { data: listData([previous, cycle(), next]) })

    expect(within(phase('Current')).getByRole('link', { name: 'Cycle 12' })).toBeInTheDocument()
    expect(within(phase('Upcoming')).getByRole('link', { name: 'Cycle 13' })).toBeInTheDocument()
    expect(within(phase('Past')).getByRole('link', { name: 'Cycle 11' })).toBeInTheDocument()

    // And in exactly one section each: a cycle counted twice is what a
    // closed interval on both ends would produce.
    expect(within(phase('Current')).queryByRole('link', { name: 'Cycle 11' })).toBeNull()
    expect(within(phase('Upcoming')).queryByRole('link', { name: 'Cycle 12' })).toBeNull()
  })

  it('states each empty phase rather than letting the section vanish', async () => {
    const view = renderApp({ initialPath: `/${WORKSPACE_SLUG}/cycles` })

    await view.link.resolve('CycleTeams', { data: teamsData })
    await view.link.resolve('CycleList', { data: listData([next]) })

    // A section that simply disappeared would leave the reader unsure
    // whether there is no current cycle or whether the screen forgot one.
    expect(within(phase('Current')).getByText('No cycle covers today.')).toBeInTheDocument()
    expect(within(phase('Past')).getByText('No cycle has finished yet.')).toBeInTheDocument()
  })
})

describe('the cycle detail', () => {
  async function openDetail(
    value: CycleFields | null,
    issues: CycleIssuesQuery = issuesData(),
  ) {
    const view = renderApp({ initialPath: `/${WORKSPACE_SLUG}/cycles/${CYCLE_ID}` })

    await view.link.resolve('CycleDetail', { data: { cycle: value } })
    await view.link.resolve('CycleIssues', { data: issues })

    if (value !== null) {
      // Only asked once the cycle has answered: the team it scopes to comes
      // from `Cycle.teamId`, which is the point of selecting it.
      await view.link.resolve('CycleUnscheduledIssues', { data: noUnscheduled })
    }

    return view
  }

  it('gives one answer to a cycle that does not exist and to one in another workspace', async () => {
    await openDetail(null)

    expect(screen.getByText('No such cycle')).toBeInTheDocument()
  })

  it('asks the server for this cycle’s issues, by the id in the route', async () => {
    const view = renderApp({ initialPath: `/${WORKSPACE_SLUG}/cycles/${CYCLE_ID}` })

    await expect(view.link.waitForRequest('CycleIssues')).resolves.toEqual({
      workspaceSlug: WORKSPACE_SLUG,
      cycleId: CYCLE_ID,
      after: null,
    })
  })

  it('scopes the issues it offers to add to the cycle’s own team', async () => {
    const view = renderApp({ initialPath: `/${WORKSPACE_SLUG}/cycles/${CYCLE_ID}` })

    await view.link.resolve('CycleDetail', { data: { cycle: cycle() } })

    // `issueSetCycle` refuses a cycle that is not the issue's team's, so a
    // menu built from another team's issues would offer moves that cannot
    // work. The team comes from the cycle, not from whichever team a picker
    // was last left on.
    await expect(view.link.waitForRequest('CycleUnscheduledIssues')).resolves.toEqual({
      workspaceSlug: WORKSPACE_SLUG,
      teamId: ENG,
    })
  })

  it('says an empty issue panel is empty, without hedging about pages', async () => {
    await openDetail(cycle())

    expect(screen.getByText('No issues in this cycle')).toBeInTheDocument()
    expect(screen.getByText('Nothing has been scheduled into it yet.')).toBeInTheDocument()
  })

  it('states how many of the cycle’s issues are on screen, and only while some are not', async () => {
    const partial = await openDetail(
      cycle(),
      issuesData([cycleIssue('Alpha')], { hasNextPage: true, totalCount: 4 }),
    )

    expect(
      within(main()).getByText('Showing 1 of 4 issues in this cycle.'),
    ).toBeInTheDocument()

    partial.unmount()

    await openDetail(cycle(), issuesData([cycleIssue('Alpha')]))

    expect(within(main()).queryByText(/issues in this cycle\./)).toBeNull()
  })

  it('deletes a cycle through the loose-argument mutation the schema declares', async () => {
    const view = await openDetail(cycle())

    await view.user.click(screen.getByRole('button', { name: 'Actions for Cycle 12' }))
    await view.user.click(screen.getByRole('menuitem', { name: 'Delete cycle' }))

    // `cycleDelete` is the one mutation here that takes loose arguments
    // rather than an input object, and it is called as declared.
    await expect(view.link.waitForRequest('CycleDelete')).resolves.toEqual({
      workspaceSlug: WORKSPACE_SLUG,
      id: CYCLE_ID,
    })
  })
})
