import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { main, renderApp } from '../../test/render'
import {
  CYCLE_ID,
  MEMBER_ID,
  PROJECT_ID,
  TEAM_ID,
  WORKSPACE_SLUG,
  workspaceContextData,
} from '../../test/factories'
import type { IssueTemplateFieldsFragment } from '../../generated/operations'

/**
 * The templates screen, against the real router, cache and a controlled
 * network.
 *
 * The claim that matters most here is a data-loss one.
 * `IssueTemplateUpdateInput.template` is an `IssueTemplateFieldsInput!` -- a
 * whole-row replace -- so an editor that submits only the fields it renders
 * does not leave the rest alone, it clears them. The test below renames a
 * template carrying an assignee, a project, a cycle and two labels and
 * asserts every one of them comes back on the wire.
 */

const TEMPLATE_ID = '00000000-0000-4000-8000-00000010a001'
const LABEL_ONE = '00000000-0000-4000-8000-00000010b001'
const LABEL_TWO = '00000000-0000-4000-8000-00000010b002'
const CREATED_ISSUE = '00000000-0000-4000-8000-00000010c001'

const TEMPLATES_PATH = `/${WORKSPACE_SLUG}/templates`

function template(
  overrides: Partial<IssueTemplateFieldsFragment> = {},
): IssueTemplateFieldsFragment {
  return {
    __typename: 'IssueTemplate',
    id: TEMPLATE_ID,
    teamId: null,
    name: 'Bug report',
    title: 'Something is broken',
    description: 'Steps to reproduce:',
    priority: 2,
    estimate: 3,
    assigneeId: null,
    projectId: null,
    cycleId: null,
    labelIds: [],
    ...overrides,
  }
}

async function openTemplates(templates: readonly IssueTemplateFieldsFragment[]) {
  const app = renderApp({ initialPath: TEMPLATES_PATH })

  await app.link.resolve('IssueWorkspaceContext', { data: workspaceContextData() })
  await app.link.resolve('IssueTemplateList', { data: { issueTemplates: [...templates] } })

  return app
}

describe('the template list', () => {
  it('lists templates under the team that owns them', async () => {
    await openTemplates([
      template(),
      template({ id: '00000000-0000-4000-8000-00000010a002', name: 'Incident', teamId: TEAM_ID }),
    ])

    // A template with no team is workspace-wide, not malformed, and gets its
    // own group rather than being hidden by a team filter.
    const shared = within(main()).getByRole('region', { name: 'Any team' })
    const eng = within(main()).getByRole('region', { name: 'ENG · Engineering' })

    expect(within(shared).getByText('Bug report')).toBeInTheDocument()
    expect(within(eng).getByText('Incident')).toBeInTheDocument()
  })

  it('says there are none rather than showing an empty list', async () => {
    await openTemplates([])

    expect(within(main()).getByText('No templates yet')).toBeInTheDocument()
  })

  it('offers a retry when the list could not be loaded', async () => {
    const app = renderApp({ initialPath: TEMPLATES_PATH })

    await app.link.resolve('IssueWorkspaceContext', { data: workspaceContextData() })
    await app.link.fail('IssueTemplateList', new Error('Network unreachable'))

    const alert = await screen.findByRole('alert')

    expect(alert).toHaveTextContent('Could not load templates')

    await app.user.click(within(alert).getByRole('button', { name: 'Try again' }))

    await expect(app.link.waitForRequest('IssueTemplateList')).resolves.toEqual({
      workspaceSlug: WORKSPACE_SLUG,
    })
  })
})

describe('editing a template', () => {
  it('re-sends the four fields it draws no control for', async () => {
    const app = await openTemplates([
      template({
        assigneeId: MEMBER_ID,
        projectId: PROJECT_ID,
        cycleId: CYCLE_ID,
        labelIds: [LABEL_ONE, LABEL_TWO],
      }),
    ])

    await app.user.click(screen.getByRole('button', { name: 'Actions on Bug report' }))
    await app.user.click(screen.getByRole('menuitem', { name: 'Edit template...' }))

    const name = await screen.findByLabelText('Template name')

    await app.user.clear(name)
    await app.user.type(name, 'Defect report')
    await app.user.click(screen.getByRole('button', { name: 'Save changes' }))

    /*
      The heart of it. `IssueTemplateFieldsInput` defaults every absent scalar
      to null and `labelIds` to `[]`, so a submission built from the six
      rendered fields would wipe the other four -- an innocent rename
      deleting an assignee, a project, a cycle and two labels.
    */
    await expect(app.link.waitForRequest('IssueTemplateUpdate')).resolves.toEqual({
      input: {
        workspaceSlug: WORKSPACE_SLUG,
        id: TEMPLATE_ID,
        template: {
          name: 'Defect report',
          teamId: null,
          title: 'Something is broken',
          description: 'Steps to reproduce:',
          priority: 2,
          estimate: 3,
          assigneeId: MEMBER_ID,
          projectId: PROJECT_ID,
          cycleId: CYCLE_ID,
          labelIds: [LABEL_ONE, LABEL_TWO],
        },
      },
    })
  })

  it('tells the reader that the fields it cannot edit are kept', async () => {
    const app = await openTemplates([template({ projectId: PROJECT_ID })])

    await app.user.click(screen.getByRole('button', { name: 'Actions on Bug report' }))
    await app.user.click(screen.getByRole('menuitem', { name: 'Edit template...' }))

    await screen.findByLabelText('Template name')

    // Said out loud, because a reader who cannot see those values has no
    // other way to know a rename did not drop them.
    expect(screen.getByRole('note')).toHaveTextContent(
      /assignee, project, cycle or labels.*kept/i,
    )
  })

  it('sends an emptied box as null rather than as an empty string', async () => {
    const app = await openTemplates([template()])

    await app.user.click(screen.getByRole('button', { name: 'Actions on Bug report' }))
    await app.user.click(screen.getByRole('menuitem', { name: 'Edit template...' }))

    const title = await screen.findByLabelText('Issue title')

    await app.user.clear(title)
    await app.user.click(screen.getByRole('button', { name: 'Save changes' }))

    const sent = await app.link.waitForRequest('IssueTemplateUpdate')
    const input = (sent as { input: { template: Record<string, unknown> } }).input

    /*
      "The template does not set a title" is null. An empty string would be a
      title the server accepts, and every issue filed from this template would
      start with a blank one.
    */
    expect(input.template['title']).toBeNull()
  })

  it('shows a refused name against the field the server named', async () => {
    const app = await openTemplates([template()])

    await app.user.click(screen.getByRole('button', { name: 'Actions on Bug report' }))
    await app.user.click(screen.getByRole('menuitem', { name: 'Edit template...' }))

    await screen.findByLabelText('Template name')
    await app.user.click(screen.getByRole('button', { name: 'Save changes' }))

    await app.link.resolve('IssueTemplateUpdate', {
      data: {
        issueTemplateUpdate: {
          __typename: 'IssueTemplateSavePayload',
          template: null,
          errors: [
            {
              __typename: 'ValidationErrorType',
              field: 'name',
              code: 'BAD_USER_INPUT',
              message: 'A template with that name already exists.',
            },
          ],
        },
      },
    })

    const field = await screen.findByLabelText('Template name')

    expect(field).toHaveAccessibleDescription('A template with that name already exists.')
    expect(field).toBeInvalid()
  })
})

describe('filing an issue from a template', () => {
  it('will not file without a team, because an issue belongs to one', async () => {
    const app = await openTemplates([template()])

    await app.user.click(screen.getByRole('button', { name: 'New issue from Bug report' }))

    /*
      `IssueCreateFromTemplateInput.teamId` is non-null and this template is
      workspace-wide, so there is nothing to infer a team from. The screen
      asks rather than guessing, and cannot submit until it is answered.
    */
    expect(await screen.findByRole('button', { name: 'Create issue' })).toBeDisabled()
  })

  it('preselects the team a template names', async () => {
    const app = await openTemplates([template({ teamId: TEAM_ID })])

    await app.user.click(screen.getByRole('button', { name: 'New issue from Bug report' }))
    await app.user.click(await screen.findByRole('button', { name: 'Create issue' }))

    await expect(app.link.waitForRequest('IssueCreateFromTemplate')).resolves.toEqual({
      input: {
        workspaceSlug: WORKSPACE_SLUG,
        templateId: TEMPLATE_ID,
        teamId: TEAM_ID,
        // Untouched, so the template's own title is used rather than an
        // override that happens to equal it.
        title: null,
      },
    })
  })

  it('sends a retitled issue as an override', async () => {
    const app = await openTemplates([template({ teamId: TEAM_ID })])

    await app.user.click(screen.getByRole('button', { name: 'New issue from Bug report' }))

    const title = await screen.findByLabelText('Title')

    await app.user.clear(title)
    await app.user.type(title, 'Export times out')
    await app.user.click(screen.getByRole('button', { name: 'Create issue' }))

    const sent = await app.link.waitForRequest('IssueCreateFromTemplate')

    expect((sent as { input: { title: string } }).input.title).toBe('Export times out')
  })

  it('links to the issue it created rather than only saying it worked', async () => {
    const app = await openTemplates([template({ teamId: TEAM_ID })])

    await app.user.click(screen.getByRole('button', { name: 'New issue from Bug report' }))
    await app.user.click(await screen.findByRole('button', { name: 'Create issue' }))

    await app.link.resolve('IssueCreateFromTemplate', {
      data: {
        issueCreateFromTemplate: {
          __typename: 'IssueCreateFromTemplatePayload',
          issue: {
            __typename: 'Issue',
            id: CREATED_ISSUE,
            identifier: 'ENG-77',
            title: 'Something is broken',
          },
          errors: [],
        },
      },
    })

    const link = await screen.findByRole('link', { name: /ENG-77/ })

    expect(link).toHaveAttribute(
      'href',
      `/${WORKSPACE_SLUG}/issues/${CREATED_ISSUE}`,
    )
  })
})
