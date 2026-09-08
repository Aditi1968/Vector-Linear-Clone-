import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import {
  expectEveryButtonNamed,
  expectHeadingLevelsUnbroken,
  expectNoDuplicateButtonNames,
  expectOneFirstLevelHeading,
} from '../../test/a11y'
import { WORKSPACE_SLUG } from '../../test/factories'
import { main, renderApp } from '../../test/render'
import type { WorkspaceAnalyticsQuery } from '../../generated/operations'

/**
 * The analytics screen, against the real router, cache and a controlled
 * network.
 *
 * The claims here are almost all about what the screen REFUSES to draw. A
 * metrics page's failure mode is not a crash, it is a plausible number: a
 * median over an unrepresentative sample, a rate of zero that means "no data",
 * a bounded table read as the whole workspace, a sum of estimates that have no
 * common unit. Every test below pins one of those refusals, because none of
 * them fails any other assertion and none of them is visible in a screenshot.
 */

const ANALYTICS_PATH = `/${WORKSPACE_SLUG}/analytics`

type Analytics = WorkspaceAnalyticsQuery['workspaceAnalytics']

const TEAM_ID = '00000000-0000-4000-8000-0000000aa001'
const PROJECT_ID = '00000000-0000-4000-8000-0000000bb001'
const CYCLE_ID = '00000000-0000-4000-8000-0000000cc001'
const ADA_ID = '00000000-0000-4000-8000-0000000dd001'

function analytics(overrides: Partial<Analytics> = {}): Analytics {
  return {
    __typename: 'WorkspaceAnalytics',
    rangeStart: '2026-03-01',
    rangeEnd: '2026-03-03',
    days: 3,
    throughput: [
      { __typename: 'ThroughputDay', day: '2026-03-01', created: 4, completed: 2, canceled: 0 },
      { __typename: 'ThroughputDay', day: '2026-03-02', created: 1, completed: 0, canceled: 0 },
      { __typename: 'ThroughputDay', day: '2026-03-03', created: 0, completed: 1, canceled: 1 },
    ],
    totals: {
      __typename: 'ThroughputTotals',
      created: 5,
      completed: 3,
      canceled: 1,
      completionRate: 0.75,
    },
    leadTime: { __typename: 'DurationSummary', count: 12, medianHours: 48, p90Hours: 96 },
    cycleTime: {
      __typename: 'CycleTimeSummary',
      measured: 8,
      completedTotal: 12,
      medianHours: 12,
      p90Hours: 30,
    },
    issueAge: { __typename: 'DurationSummary', count: 20, medianHours: 300, p90Hours: 900 },
    stateMix: [
      { __typename: 'StateCategoryCount', category: 'STARTED', issues: 3 },
      { __typename: 'StateCategoryCount', category: 'BACKLOG', issues: 9 },
    ],
    priorityMix: [
      { __typename: 'PriorityCount', priority: 0, issues: 7 },
      { __typename: 'PriorityCount', priority: 1, issues: 2 },
    ],
    workload: [
      { __typename: 'AssigneeWorkload', assigneeId: ADA_ID, name: 'Ada', openIssues: 6 },
      { __typename: 'AssigneeWorkload', assigneeId: null, name: null, openIssues: 4 },
    ],
    assigneeTotal: 34,
    teams: [
      {
        __typename: 'TeamCompletion',
        teamId: TEAM_ID,
        key: 'CORE',
        name: 'Core',
        estimateScale: 'POINTS',
        completed: 3,
        estimated: 3,
        estimateTotal: 21,
      },
    ],
    teamTotal: 1,
    projects: [
      {
        __typename: 'ProjectProgress',
        projectId: PROJECT_ID,
        name: 'Launch',
        state: 'started',
        issues: 10,
        completed: 4,
      },
    ],
    projectTotal: 3,
    cycles: [
      {
        __typename: 'CycleProgress',
        cycleId: CYCLE_ID,
        number: 7,
        name: 'Sprint 7',
        startsAt: '2026-02-23T00:00:00Z',
        endsAt: '2026-03-09T00:00:00Z',
        teamKey: 'CORE',
        estimateScale: 'POINTS',
        issues: 14,
        completed: 6,
        estimated: 5,
        completedEstimate: 21,
      },
    ],
    cycleTotal: 2,
    overdue: 5,
    ...overrides,
  }
}

async function openAnalytics(overrides: Partial<Analytics> = {}) {
  const app = renderApp({ initialPath: ANALYTICS_PATH })

  await app.link.resolve('WorkspaceAnalytics', {
    data: { workspaceAnalytics: analytics(overrides) },
  })

  return app
}

describe('the analytics screen', () => {
  it('asks the server for the aggregate rather than computing one from issues', async () => {
    const app = renderApp({ initialPath: ANALYTICS_PATH })

    /*
      The claim that made this screen buildable at all. Every figure comes
      from one field that aggregates over the whole tenant; a screen that
      fetched `issues` and counted them would be reporting one page of rows as
      the workspace's throughput.
    */
    await expect(app.link.waitForRequest('WorkspaceAnalytics')).resolves.toEqual({
      workspaceSlug: WORKSPACE_SLUG,
      days: 30,
    })
  })

  it('shows the four flow figures the window supports', async () => {
    await openAnalytics()

    const flow = within(main()).getByRole('region', { name: 'Flow' })

    // Read as label-to-value pairs rather than as bare numbers: two tiles can
    // legitimately hold the same figure, and a test that matched on the digit
    // alone would pass while the two were swapped.
    const tileValue = (label: string) =>
      within(flow).getByText(label).parentElement?.querySelector('dd')?.textContent

    expect(tileValue('Issues created')).toBe('5')
    expect(tileValue('Issues completed')).toBe('3')
    expect(tileValue('Issues canceled')).toBe('1')
    expect(tileValue('Completion rate')).toBe('75%')
    expect(tileValue('Overdue')).toBe('5')
  })

  it('says the days are UTC instead of leaving a reader to discover it', async () => {
    await openAnalytics()

    // A real and permanent skew: a completion at 23:00 in Los Angeles lands on
    // the next day's bar, and nothing in the schema records a viewer's zone.
    expect(within(main()).getByText(/days are utc/i)).toBeInTheDocument()
  })

  it('states the widest window the server will answer', async () => {
    await openAnalytics()

    // The bound is enforced server-side and refused rather than clamped, so
    // the screen has to say what it is or the picker looks arbitrary. Matched
    // on the sentence rather than on "180 days", which is also the text of an
    // option in the picker.
    expect(
      within(main()).getByText(/widest window this server will answer is 180 days/i),
    ).toBeInTheDocument()
  })
})

describe('the honesty of each figure', () => {
  it('prints the coverage beside a partial cycle time', async () => {
    await openAnalytics()

    // 8 of 12 measurable. Without this the median reads as though it covered
    // every delivered issue, and the ones it covers are the ones that were
    // tracked most carefully.
    expect(within(main()).getByText(/8 of the 12 issues delivered/i)).toBeInTheDocument()
  })

  it('refuses to show a cycle time when nothing could be measured', async () => {
    await openAnalytics({
      cycleTime: {
        __typename: 'CycleTimeSummary',
        measured: 0,
        completedTotal: 96,
        medianHours: 0,
        p90Hours: 0,
      },
    })

    /*
      The server still sends a summary here -- carrying zeros -- so that
      "0 of 96" can be reported. Rendering its median would print "0h", which
      reads as instant delivery: the most flattering possible misreading of a
      measurement that does not exist.
    */
    expect(within(main()).getByText(/none of the 96 issues delivered/i)).toBeInTheDocument()
    expect(within(main()).queryByText('0h')).not.toBeInTheDocument()
  })

  it('renders an absent completion rate as absent and not as zero', async () => {
    await openAnalytics({
      totals: {
        __typename: 'ThroughputTotals',
        created: 9,
        completed: 0,
        canceled: 0,
        completionRate: null,
      },
    })

    // A fortnight in which nothing was abandoned either has no delivery
    // share. "0%" would report a failure that did not happen.
    expect(within(main()).getByText(/not a rate of zero/i)).toBeInTheDocument()
    expect(within(main()).queryByText('0%')).not.toBeInTheDocument()
  })

  it('warns when a percentile is over too small a sample', async () => {
    await openAnalytics({
      leadTime: { __typename: 'DurationSummary', count: 2, medianHours: 4, p90Hours: 6 },
    })

    expect(within(main()).getByText(/too few to read a trend into/i)).toBeInTheDocument()
  })

  it('says what a bounded table is not showing', async () => {
    await openAnalytics()

    /*
      Every list on this screen is capped server-side and arrives beside a
      total. A table that did not say "2 of 34" would read as the whole
      workspace, which is the quietest way a bounded aggregate misleads.
    */
    expect(within(main()).getByText(/2 of 34 assignees/i)).toBeInTheDocument()
    expect(within(main()).getByText(/1 of 3 projects/i)).toBeInTheDocument()
  })
})

describe('mixed estimate scales', () => {
  it('refuses a workspace total when two teams use different units', async () => {
    await openAnalytics({
      teams: [
        {
          __typename: 'TeamCompletion',
          teamId: TEAM_ID,
          key: 'CORE',
          name: 'Core',
          estimateScale: 'POINTS',
          completed: 3,
          estimated: 3,
          estimateTotal: 21,
        },
        {
          __typename: 'TeamCompletion',
          teamId: '00000000-0000-4000-8000-0000000aa002',
          key: 'DES',
          name: 'Design',
          estimateScale: 'TSHIRT',
          completed: 2,
          estimated: 2,
          estimateTotal: null,
        },
      ],
      teamTotal: 2,
    })

    const delivery = within(main()).getByRole('region', { name: 'Delivery by team' })

    expect(within(delivery).getByText('21 points')).toBeInTheDocument()

    /*
      The t-shirt team is SIZED and has no total, and the two facts are shown
      separately. Collapsing them into "not sized" would tell a team that
      estimates every issue that it estimates nothing.
    */
    expect(within(delivery).getByText(/2 sized — t-shirt sizes do not sum/i)).toBeInTheDocument()
    expect(within(main()).getByText(/no common measure/i)).toBeInTheDocument()
  })

  it('does not explain a total it was never going to show when the scales agree', async () => {
    await openAnalytics()

    // A caveat shown unconditionally is a caveat readers learn to skip.
    expect(within(main()).queryByText(/no common measure/i)).not.toBeInTheDocument()
    expect(within(main()).getByText(/summed within a team/i)).toBeInTheDocument()
  })
})

describe('the charts', () => {
  it('draws the throughput series as an image with a text alternative', async () => {
    await openAnalytics()

    // `role="img"` with a name carrying the shape of the series. A 180-row
    // table is not an alternative to this chart, it is 180 rows nobody reads.
    const chart = within(main()).getByRole('img', { name: /daily throughput over 3 days/i })

    expect(chart).toBeInTheDocument()
    expect(chart).toHaveAccessibleName(/busiest day was 2026-03-01/i)
  })

  it('says a flat stretch is a quiet week and not missing data', async () => {
    await openAnalytics()

    // An empty chart and a chart of zeros look identical, and the server
    // zero-fills precisely so this distinction can be made.
    expect(within(main()).getByText(/a real quiet week and not missing data/i)).toBeInTheDocument()
  })

  it('is a table as well as a chart, so each bar has its number beside it', async () => {
    await openAnalytics()

    const byStatus = within(main()).getByRole('table', { name: /issues by status/i })

    // Every breakdown IS its table -- there is no "show as table" disclosure,
    // which is what keeps four controls off the page sharing one name.
    expect(within(byStatus).getByRole('rowheader', { name: 'In progress' })).toBeInTheDocument()
    expect(within(byStatus).getByRole('rowheader', { name: 'Done' })).toBeInTheDocument()
  })

  it('gives an empty status category a zero rather than no row', async () => {
    await openAnalytics({ stateMix: [] })

    const byStatus = within(main()).getByRole('table', { name: /issues by status/i })

    // Five rows whatever the server sent: a chart missing a bar reads as a
    // chart with fewer categories.
    expect(within(byStatus).getAllByRole('rowheader')).toHaveLength(5)
  })

  it('keeps the unassigned pile as a row rather than dropping it', async () => {
    await openAnalytics()

    const byAssignee = within(main()).getByRole('table', { name: /open issues by assignee/i })

    // Usually the largest bucket, and the one a workload chart most needs to
    // show. Dropping it would make the bars add up to less than the board.
    expect(within(byAssignee).getByRole('rowheader', { name: 'Unassigned' })).toBeInTheDocument()
  })
})

describe('loading, failing and changing the window', () => {
  it('keeps the previous window on screen while a wider one loads', async () => {
    const app = await openAnalytics()

    await app.user.selectOptions(screen.getByRole('combobox', { name: 'Date range' }), '90')

    await expect(app.link.waitForRequest('WorkspaceAnalytics')).resolves.toEqual({
      workspaceSlug: WORKSPACE_SLUG,
      days: 90,
    })

    /*
      Dimmed, not blanked. The reader asked to compare two ranges, and
      replacing the first one with a skeleton is exactly what stops them.
    */
    expect(within(main()).getByText(/still the previous one/i)).toBeInTheDocument()
  })

  it('reports a failure instead of drawing an empty chart', async () => {
    const app = renderApp({ initialPath: ANALYTICS_PATH })

    await app.link.fail('WorkspaceAnalytics', new Error('nope'))

    // A chart of zeros over a failed request is the worst possible rendering:
    // it says the workspace did nothing.
    expect(await screen.findByText('Could not load the analytics')).toBeInTheDocument()
  })

  it('says nothing was delivered rather than drawing an empty team table', async () => {
    await openAnalytics({ teams: [], teamTotal: 0 })

    expect(within(main()).getByText('Nothing was delivered in this window')).toBeInTheDocument()
  })
})

describe('analytics is navigable without a mouse', () => {
  it('names every button, once', async () => {
    await openAnalytics()

    expectEveryButtonNamed()
    expectNoDuplicateButtonNames()
  })

  it('has one first-level heading and an unbroken outline', async () => {
    await openAnalytics()

    // Each section is an `<h2>` under the page title -- one level below the
    // `<h1>` and no deeper.
    expectOneFirstLevelHeading('Analytics')
    expectHeadingLevelsUnbroken()
  })

  it('gives every table a distinct accessible name', async () => {
    await openAnalytics()

    /*
      The defect this codebase keeps re-shipping is two controls with one name
      on a screen. The same hazard applies to the six tables here: `getByRole`
      throws on multiple matches, so a duplicated caption would fail the
      queries above -- and this asserts it directly rather than by side effect.
    */
    const names = within(main())
      .getAllByRole('table')
      .map((table) => table.querySelector('caption')?.textContent ?? '')

    expect(new Set(names).size).toBe(names.length)
  })
})
