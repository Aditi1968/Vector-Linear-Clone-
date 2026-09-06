import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { main, renderApp } from '../../test/render'
import { WORKSPACE_SLUG } from '../../test/factories'
import type {
  ProjectDetailData,
  ProjectDetailFields,
  ProjectIssuesData,
  ProjectListData,
  ProjectMilestone,
  ProjectTeamsData,
} from './api'
import type { ProjectMembersQuery } from '../../generated/operations'

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
      // The list document selects a subset of the detail fields, so a detail
      // fixture satisfies it; the extra fields are simply not read.
      nodes: nodes.map(({ description: _d, createdAt: _c, milestones: _m, ...row }) => row),
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

const noIssues: ProjectIssuesData = {
  issues: {
    __typename: 'IssueConnection',
    nodes: [],
    pageInfo: { __typename: 'PageInfo', hasNextPage: false, endCursor: null },
  },
}

/** Answer the three queries the detail screen fires, in any order. */
async function openDetail(value: ProjectDetailFields | null) {
  const view = renderApp({ initialPath: `/${WORKSPACE_SLUG}/projects/${PROJECT_ID}` })

  await view.link.resolve('ProjectDetail', { data: detailData(value) })
  await view.link.resolve('ProjectTeams', { data: teamsData })
  await view.link.resolve('ProjectMembers', { data: membersData })
  await view.link.resolve('ProjectIssues', { data: noIssues })

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

  it('says an empty issue panel is empty, not broken', async () => {
    await openDetail(project())

    // The API has no per-project issue filter, so an empty panel means "none
    // among those loaded". The screen has to be explicit about which.
    expect(screen.getByText('No issues in this project yet')).toBeInTheDocument()
  })
})
