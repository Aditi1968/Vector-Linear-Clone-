import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import {
  expectEveryButtonNamed,
  expectHeadingLevelsUnbroken,
  expectNoDuplicateButtonNames,
  expectOneFirstLevelHeading,
} from '../../test/a11y'
import { main, renderApp } from '../../test/render'
import { PROJECT_ID, WORKSPACE_SLUG } from '../../test/factories'
import type {
  RoadmapInitiativeFieldsFragment,
  RoadmapProjectFieldsFragment,
} from '../../generated/operations'
import { groupByMonth, isOverdue, toRoadmapItems } from './lib/roadmap'

/**
 * The roadmap, against the real router, cache and a controlled network.
 *
 * The claim this file exists to pin is a negative one: the roadmap does not
 * draw a span. `Initiative.targetDate` and `Project.targetDate` are the only
 * scheduling fields in the schema -- one date each, no start -- so an item is
 * a marker in a month and never a bar, and an item with no date is listed
 * rather than dropped off a calendar it cannot sit on.
 */

const ROADMAP_PATH = `/${WORKSPACE_SLUG}/roadmap`

const INITIATIVE_ID = '00000000-0000-4000-8000-0000000ee001'

function roadmapInitiative(
  overrides: Partial<RoadmapInitiativeFieldsFragment> = {},
): RoadmapInitiativeFieldsFragment {
  return {
    __typename: 'Initiative',
    id: INITIATIVE_ID,
    name: 'Q3 launch',
    status: 'ACTIVE',
    health: null,
    targetDate: '2026-09-30',
    ...overrides,
  }
}

function roadmapProject(
  overrides: Partial<RoadmapProjectFieldsFragment> = {},
): RoadmapProjectFieldsFragment {
  return {
    __typename: 'Project',
    id: PROJECT_ID,
    name: 'Platform',
    state: 'STARTED',
    health: 'AT_RISK',
    targetDate: '2026-10-15',
    ...overrides,
  }
}

async function openRoadmap(
  initiatives: readonly RoadmapInitiativeFieldsFragment[],
  projects: readonly RoadmapProjectFieldsFragment[],
  {
    initiativesHaveMore = false,
    projectsHaveMore = false,
  }: { initiativesHaveMore?: boolean; projectsHaveMore?: boolean } = {},
) {
  const app = renderApp({ initialPath: ROADMAP_PATH })

  await app.link.resolve('RoadmapInitiatives', {
    data: {
      initiatives: {
        __typename: 'InitiativeConnection',
        nodes: [...initiatives],
        pageInfo: {
          __typename: 'PageInfo',
          hasNextPage: initiativesHaveMore,
          endCursor: initiativesHaveMore ? 'initiative-cursor' : null,
        },
      },
    },
  })

  await app.link.resolve('RoadmapProjects', {
    data: {
      projects: {
        __typename: 'ProjectConnection',
        nodes: [...projects],
        pageInfo: {
          __typename: 'PageInfo',
          hasNextPage: projectsHaveMore,
          endCursor: projectsHaveMore ? 'project-cursor' : null,
        },
      },
    },
  })

  return app
}

describe('the roadmap', () => {
  it('puts each item in the month it is due, from both lists', async () => {
    await openRoadmap([roadmapInitiative()], [roadmapProject()])

    const september = within(main()).getByRole('region', { name: /September 2026/ })
    const october = within(main()).getByRole('region', { name: /October 2026/ })

    expect(within(september).getByText('Q3 launch')).toBeInTheDocument()
    expect(within(october).getByText('Platform')).toBeInTheDocument()

    /*
      Which list a row came from is on every row. The roadmap is the one
      screen where initiatives and projects appear together, and a reader who
      cannot tell them apart cannot tell a launch from a project inside it.
    */
    expect(within(september).getByText('Initiative')).toBeInTheDocument()
    expect(within(october).getByText('Project')).toBeInTheDocument()
  })

  it('lists what has no target date instead of leaving it off', async () => {
    await openRoadmap([roadmapInitiative({ targetDate: null, name: 'Unscheduled work' })], [])

    const undated = within(main()).getByRole('region', { name: /Not scheduled/ })

    // The single most useful thing a roadmap can point at, and exactly what a
    // calendar-shaped view drops unless somebody decides otherwise.
    expect(within(undated).getByText('Unscheduled work')).toBeInTheDocument()
  })

  it('says out loud that these are markers and not spans', async () => {
    await openRoadmap([roadmapInitiative()], [])

    // Said rather than left to be inferred from a chart that looks like a
    // timeline and is not one.
    expect(
      within(main()).getByText(/no start date in the schema/i),
    ).toBeInTheDocument()
  })

  it('advances both lists when either has more', async () => {
    const app = await openRoadmap([roadmapInitiative()], [roadmapProject()], {
      initiativesHaveMore: true,
      projectsHaveMore: true,
    })

    await app.user.click(screen.getByRole('button', { name: 'Load more' }))

    await expect(app.link.waitForRequest('RoadmapInitiatives')).resolves.toEqual({
      workspaceSlug: WORKSPACE_SLUG,
      after: 'initiative-cursor',
    })
    await expect(app.link.waitForRequest('RoadmapProjects')).resolves.toEqual({
      workspaceSlug: WORKSPACE_SLUG,
      after: 'project-cursor',
    })
  })

  it('says there is nothing to schedule rather than drawing an empty calendar', async () => {
    await openRoadmap([], [])

    expect(within(main()).getByText('Nothing to schedule yet')).toBeInTheDocument()
  })
})

describe('grouping items into months', () => {
  it('orders months by date and not by the order the items arrived', () => {
    const items = toRoadmapItems(
      [
        roadmapInitiative({ id: INITIATIVE_ID, targetDate: '2026-12-01' }),
        roadmapInitiative({ id: 'b', targetDate: '2026-02-01' }),
      ],
      [],
    )

    expect(groupByMonth(items).months.map((month) => month.key)).toEqual([
      '2026-02',
      '2026-12',
    ])
  })

  it('does not call a completed project overdue', () => {
    const shipped = toRoadmapItems(
      [],
      [roadmapProject({ state: 'COMPLETED', targetDate: '2020-01-01' })],
    )[0]

    if (shipped === undefined) {
      throw new Error('toRoadmapItems dropped the project it was given')
    }

    /*
      A project that shipped in 2020 is not late, it is done. A red flag on it
      is noise that teaches people to ignore the flag.
    */
    expect(isOverdue(shipped, Date.parse('2026-09-08T00:00:00Z'))).toBe(false)
  })

  it('calls an unfinished past-due project overdue', () => {
    const late = toRoadmapItems([], [roadmapProject({ targetDate: '2026-01-01' })])[0]

    if (late === undefined) {
      throw new Error('toRoadmapItems dropped the project it was given')
    }

    expect(isOverdue(late, Date.parse('2026-09-08T00:00:00Z'))).toBe(true)
  })
})

describe('the roadmap is navigable without a mouse', () => {
  it('names every button, once', async () => {
    await openRoadmap([roadmapInitiative()], [roadmapProject()], {
      initiativesHaveMore: true,
    })

    expectEveryButtonNamed()
    expectNoDuplicateButtonNames()
  })

  it('has one first-level heading and an unbroken outline', async () => {
    await openRoadmap(
      [roadmapInitiative(), roadmapInitiative({ id: 'b', targetDate: null })],
      [roadmapProject()],
    )

    // Each month is a section under the page title, so the months are `<h2>`
    // -- one level below the `<h1>` and no deeper.
    expectOneFirstLevelHeading('Roadmap')
    expectHeadingLevelsUnbroken()
  })
})
