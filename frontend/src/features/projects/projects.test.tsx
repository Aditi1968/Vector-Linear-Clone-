import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { main, renderApp } from '../../test/render'
import { WORKSPACE_SLUG, issueDetail, issueProjectSet } from '../../test/factories'
import type {
  ProjectDetailData,
  ProjectDetailFields,
  ProjectIssuesData,
  ProjectListData,
  ProjectMilestone,
  ProjectTeamsData,
} from './api'
import type {
  ProjectMembersQuery,
  ProjectUnfiledIssuesQuery as ProjectUnfiledIssuesData,
} from '../../generated/operations'

/**
 * The project screens, against the real router, the real cache and a
 * controlled network.
 *
 * Everything below the link is production code, the cache included -- the
 * `projects` field policy is what makes "Load more" work at all, and a test
 * that swapped in a bare cache would still render rows while proving nothing.
 */

const PROJECT_ID = '00000000-0000-4000-8000-0000000000a1'
const MILESTONE_ID = '00000000-0000-4000-8000-0000000000b1'
const ENG = '00000000-0000-4000-8000-0000000000c1'
const DES = '00000000-0000-4000-8000-0000000000c2'

function milestone(overrides: Partial<ProjectMilestone> = {}): ProjectMilestone {
  return {
    __typename: 'ProjectMilestone',
    id: MILESTONE_ID,
    projectId: PROJECT_ID,
    name: 'Beta',
    targetDate: null,
    position: 0,
    ...overrides,
  }
}

function project(overrides: Partial<ProjectDetailFields> = {}): ProjectDetailFields {
  return {
    __typename: 'Project',
    id: PROJECT_ID,
    name: 'Payments migration',
    description: null,
    state: 'STARTED',
    targetDate: '2026-03-14',
    leadId: null,
    teamIds: [ENG, DES],
    createdAt: '2026-01-01T00:00:00.000Z',
    updatedAt: '2026-01-01T00:00:00.000Z',
    milestones: [],
    ...overrides,
  }
}

function listData(nodes: readonly ProjectDetailFields[]): ProjectListData {
  return {
    projects: {
      __typename: 'ProjectConnection',
      // The row selection is a strict subset of the detail selection, so the
      // extra fields are dropped rather than sent. Built by picking rather
      // than by destructuring the rest, because the fields to keep are the
      // contract and a rest-spread would state the ones to discard.
      nodes: nodes.map((node) => ({
        __typename: node.__typename,
        id: node.id,
        name: node.name,
        state: node.state,
        targetDate: node.targetDate,
        leadId: node.leadId,
        teamIds: node.teamIds,
        updatedAt: node.updatedAt,
      })),
      pageInfo: { __typename: 'PageInfo', hasNextPage: false, endCursor: null },
    },
  }
}

function detailData(value: ProjectDetailFields | null): ProjectDetailData {
  return { project: value }
}

const teamsData: ProjectTeamsData = {
  teams: [
    { __typename: 'Team', id: ENG, key: 'ENG', name: 'Engineering' },
    { __typename: 'Team', id: DES, key: 'DES', name: 'Design' },
  ],
}

const membersData: ProjectMembersQuery = {
  workspaceMembers: [
    { __typename: 'WorkspaceMember', userId: 'u1', name: 'Ada', email: 'ada@example.test' },
  ],
}

/** One page of the project's issues, as the server would answer the filter. */
function issuesData(
  nodes: ProjectIssuesData['issues']['nodes'] = [],
  { hasNextPage = false, totalCount = nodes.length } = {},
): ProjectIssuesData {
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

function projectIssue(id: string, title: string): ProjectIssuesData['issues']['nodes'][number] {
  return {
    __typename: 'Issue',
    id,
    identifier: 'ENG-1',
    title,
    completedAt: null,
    projectId: PROJECT_ID,
    milestoneId: null,
  }
}

const noUnfiled: ProjectUnfiledIssuesData = {
  issues: { __typename: 'IssueConnection', nodes: [] },
}

/** Answer the queries the detail screen fires, in any order. */
async function openDetail(
  value: ProjectDetailFields | null,
  issues: ProjectIssuesData = issuesData(),
) {
  const view = renderApp({ initialPath: `/${WORKSPACE_SLUG}/projects/${PROJECT_ID}` })

  await view.link.resolve('ProjectDetail', { data: detailData(value) })
  await view.link.resolve('ProjectTeams', { data: teamsData })
  await view.link.resolve('ProjectMembers', { data: membersData })
  await view.link.resolve('ProjectIssues', { data: issues })
  await view.link.resolve('ProjectUnfiledIssues', { data: noUnfiled })

  return view
}

describe('the project list', () => {
  it('asks for the workspace in the URL', async () => {
    const view = renderApp({ initialPath: `/${WORKSPACE_SLUG}/projects` })

    // The one thing a tenant-scoped screen must get right: the slug comes
    // from the address bar and nowhere else.
    await expect(view.link.waitForRequest('ProjectList')).resolves.toMatchObject({
      workspaceSlug: WORKSPACE_SLUG,
    })
  })

  it('says a workspace has no projects rather than showing a blank page', async () => {
    const view = renderApp({ initialPath: `/${WORKSPACE_SLUG}/projects` })

    await view.link.resolve('ProjectList', { data: listData([]) })
    await view.link.resolve('ProjectTeams', { data: teamsData })
    await view.link.resolve('ProjectMembers', { data: membersData })

    expect(within(main()).getByText('No projects yet')).toBeInTheDocument()
    // The empty state offers the one action that changes the situation.
    expect(
      within(main()).getByRole('button', { name: 'Create the first project' }),
    ).toBeInTheDocument()
  })

  it('shows every team a project spans, not one of them', async () => {
    const view = renderApp({ initialPath: `/${WORKSPACE_SLUG}/projects` })

    await view.link.resolve('ProjectList', { data: listData([project()]) })
    await view.link.resolve('ProjectTeams', { data: teamsData })
    await view.link.resolve('ProjectMembers', { data: membersData })

    const row = within(main()).getByRole('listitem')

    expect(within(row).getByText('ENG')).toBeInTheDocument()
    expect(within(row).getByText('DES')).toBeInTheDocument()
  })

  it('states a project’s state on the row in words', async () => {
    const view = renderApp({ initialPath: `/${WORKSPACE_SLUG}/projects` })

    await view.link.resolve('ProjectList', { data: listData([project()]) })
    await view.link.resolve('ProjectTeams', { data: teamsData })
    await view.link.resolve('ProjectMembers', { data: membersData })

    // The state moved from a pill at the trailing edge to a mono label under
    // the name, and the thing that must survive that is the *word*: the hue
    // is a second channel and never the only one.
    const row = within(main()).getByRole('listitem')

    expect(within(row).getByText('In progress')).toBeInTheDocument()
  })

  it('leads to the project it names', async () => {
    const view = renderApp({ initialPath: `/${WORKSPACE_SLUG}/projects` })

    await view.link.resolve('ProjectList', { data: listData([project()]) })
    await view.link.resolve('ProjectTeams', { data: teamsData })
    await view.link.resolve('ProjectMembers', { data: membersData })

    // A real link, so it is keyboard-reachable and openable in a new tab --
    // asserted as a role, so a change to a `<div onClick>` fails here.
    expect(within(main()).getByRole('link', { name: 'Payments migration' })).toHaveAttribute(
      'href',
      `/${WORKSPACE_SLUG}/projects/${PROJECT_ID}`,
    )
  })
})

describe('the project detail', () => {
  it('gives one answer to a project that does not exist and to one in another workspace', async () => {
    // `project(id:)` returns null for both, and so does this screen --
    // distinguishing them would leak whether an id exists to someone who
    // cannot see it.
    await openDetail(null)

    expect(screen.getByText('No such project')).toBeInTheDocument()
  })

  it('draws the health dial with a reading rather than with raw path data', async () => {
    await openDetail(
      project(),
      issuesData([
        projectIssue('00000000-0000-4000-8000-0000000000e1', 'Alpha'),
        {
          ...projectIssue('00000000-0000-4000-8000-0000000000e2', 'Beta'),
          completedAt: '2026-02-01T00:00:00.000Z',
        },
      ]),
    )

    // Found by name, which also proves there is exactly one control with it
    // on the screen -- `getByRole` throws on two, and two progress readings
    // sharing a name is the bug this assertion is here to catch.
    const dial = within(main()).getByRole('progressbar', {
      name: 'Payments migration: closed issues',
    })

    // The counts the user is tracking, not the percentage a reader would
    // compute off `aria-valuenow`.
    expect(dial).toHaveAttribute('aria-valuetext', '1 of 2')
    // A ring is not accessible because it renders: the drawing is hidden and
    // the percentage sits beside it as text.
    expect(dial.querySelector('svg')).toHaveAttribute('aria-hidden', 'true')
    expect(dial).toHaveTextContent('50%')
  })

  it('adds a team through the mutation the schema provides for it', async () => {
    const view = await openDetail(project({ teamIds: [ENG] }))

    // A `Menu` with children is named by its visible text, not by its
    // `label` -- the label is the fallback for the icon-only form.
    await view.user.click(screen.getByRole('button', { name: 'Add team' }))
    await view.user.click(screen.getByRole('menuitem', { name: /DES/ }))

    await expect(view.link.waitForRequest('ProjectTeamAdd')).resolves.toEqual({
      input: { workspaceSlug: WORKSPACE_SLUG, projectId: PROJECT_ID, teamId: DES },
    })
  })

  it('removes a team by the tag that names it', async () => {
    const view = await openDetail(project())

    await view.user.click(screen.getByRole('button', { name: 'Remove ENG' }))

    await expect(view.link.waitForRequest('ProjectTeamRemove')).resolves.toEqual({
      input: { workspaceSlug: WORKSPACE_SLUG, projectId: PROJECT_ID, teamId: ENG },
    })
  })

  it('creates a milestone and refetches the project that owns it', async () => {
    const view = await openDetail(project())

    expect(screen.getByText('No milestones')).toBeInTheDocument()

    await view.user.click(screen.getByRole('button', { name: 'Add milestone' }))
    await view.user.type(screen.getByLabelText('Milestone name'), 'Beta')
    await view.user.click(screen.getByRole('button', { name: 'Add milestone' }))

    await expect(view.link.waitForRequest('ProjectMilestoneCreate')).resolves.toEqual({
      input: { workspaceSlug: WORKSPACE_SLUG, projectId: PROJECT_ID, name: 'Beta', targetDate: null },
    })

    await view.link.resolve('ProjectMilestoneCreate', {
      data: {
        projectMilestoneCreate: {
          __typename: 'ProjectMilestonePayload',
          milestone: milestone(),
          errors: [],
        },
      },
    })

    // The mutation returns the milestone but not the project's milestone
    // *list*, which is a plain field on the project, so the list only becomes
    // correct because the detail query is refetched. Without this the new
    // milestone would not appear until a reload.
    await view.link.resolve('ProjectDetail', {
      data: detailData(project({ milestones: [milestone()] })),
    })

    expect(screen.getByRole('heading', { name: 'Beta' })).toBeInTheDocument()
  })

  it('deletes a milestone through its own menu', async () => {
    const view = await openDetail(project({ milestones: [milestone()] }))

    await view.user.click(screen.getByRole('button', { name: 'Actions for Beta' }))
    await view.user.click(screen.getByRole('menuitem', { name: 'Delete milestone' }))

    await expect(view.link.waitForRequest('ProjectMilestoneDelete')).resolves.toEqual({
      input: { workspaceSlug: WORKSPACE_SLUG, id: MILESTONE_ID },
    })
  })

  it('asks the server for this project’s issues, and for the unfiled ones', async () => {
    const view = renderApp({ initialPath: `/${WORKSPACE_SLUG}/projects/${PROJECT_ID}` })

    // The panel's list: filtered by the id in the route, so it does not wait
    // for the project to load and does not sift the workspace for it.
    await expect(view.link.waitForRequest('ProjectIssues')).resolves.toEqual({
      workspaceSlug: WORKSPACE_SLUG,
      projectId: PROJECT_ID,
      after: null,
    })

    // The "add an issue" menu's list is the opposite question, and the null
    // is the filter rather than the absence of one -- it is written into the
    // document, so what has to be checked is that the two are separate
    // requests and this one carries no project id of its own.
    await expect(view.link.waitForRequest('ProjectUnfiledIssues')).resolves.toEqual({
      workspaceSlug: WORKSPACE_SLUG,
    })
  })

  it('says an empty issue panel is empty, without hedging about pages', async () => {
    await openDetail(project())

    expect(screen.getByText('No issues in this project yet')).toBeInTheDocument()
    expect(
      screen.getByText('Nothing in this workspace is filed against this project.'),
    ).toBeInTheDocument()
  })

  it('asks for the panel’s list again when an issue leaves the project', async () => {
    const view = await openDetail(
      project(),
      issuesData([projectIssue('00000000-0000-4000-8000-0000000000e1', 'Alpha')]),
    )

    await view.user.click(screen.getByRole('button', { name: 'Actions for ENG-1' }))
    await view.user.click(screen.getByRole('menuitem', { name: 'Remove from project' }))
    await view.link.resolve('IssueSetProject', {
      data: issueProjectSet(issueDetail(1, { project: null })),
    })

    // The panel reads a server-filtered connection, so correcting the issue's
    // own `projectId` moves it out of nothing: without the refetch the row
    // would sit in the list until a reload, which is the kind of staleness
    // that reads as "the button did not work".
    await expect(view.link.waitForRequest('ProjectIssues')).resolves.toMatchObject({
      projectId: PROJECT_ID,
    })
  })

  it('states how many of the project’s issues are on screen, and only while some are not', async () => {
    const partial = await openDetail(
      project(),
      issuesData([projectIssue('00000000-0000-4000-8000-0000000000e1', 'Alpha')], {
        hasNextPage: true,
        totalCount: 7,
      }),
    )

    // Server-side filtering does not make a paginated list complete, so the
    // honest sentence survives -- around `totalCount`, which is now a fact.
    expect(
      within(main()).getByText('Showing 1 of 7 issues in this project.'),
    ).toBeInTheDocument()

    partial.unmount()

    await openDetail(
      project(),
      issuesData([projectIssue('00000000-0000-4000-8000-0000000000e1', 'Alpha')]),
    )

    // And when the whole answer is loaded there is nothing to say about it.
    expect(within(main()).queryByText(/issues in this project\./)).toBeNull()
  })
})
