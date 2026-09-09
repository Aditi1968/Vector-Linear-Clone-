import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { LabelsPanel } from './index'
import {
  ISSUE_ID,
  issueLabelsData,
  label,
  renderPanel,
  WORKSPACE_SLUG,
  workspaceLabelsData,
} from './testHarness'

/**
 * Creating a label inline is two writes, and only the second one can fail
 * after the first has landed.
 *
 * The schema has no create-and-attach operation, so `createAndAttach` sends
 * `labelCreate` and then `issueLabelAttach`. When the second refuses, a real
 * label exists in the workspace that the person was never told about --
 * and the message they were shown ("That did not go through") says the
 * opposite.
 *
 * What made it more than a wording complaint is the move it invites. The form
 * stays open with the name still in the box, so the obvious next action is to
 * press Create again -- which now meets `labels_workspace_name_key` refusing
 * a duplicate of a label they believe does not exist. Two failures, neither
 * of which describes what actually happened.
 */
describe('a label created but not attached', () => {
  it('says the label exists rather than reporting nothing happened', async () => {
    const view = renderPanel(
      <LabelsPanel workspaceSlug={WORKSPACE_SLUG} issueId={ISSUE_ID} />,
    )

    await view.link.resolve('IssueLabels', { data: issueLabelsData([]) })
    await view.link.resolve('WorkspaceLabels', { data: workspaceLabelsData([]) })

    await view.user.click(screen.getByRole('button', { name: 'Add' }))
    await view.user.click(screen.getByRole('menuitem', { name: 'New label' }))
    await view.user.type(
      screen.getByRole('textbox', { name: 'New label name' }),
      'infra',
    )
    await view.user.click(screen.getByRole('button', { name: 'Create' }))

    // The first write lands.
    await view.link.resolve('LabelCreate', {
      data: {
        labelCreate: {
          __typename: 'LabelPayload' as const,
          label: label(3, { name: 'infra' }),
          errors: [],
        },
      },
    })

    // The second does not. A transport failure rather than a payload
    // rejection, because that is the channel with no field to blame -- and
    // the one where "nothing happened" is the easiest wrong conclusion.
    await view.link.fail('IssueLabelAttach', new Error('Failed to fetch'))

    const message = await screen.findByRole('alert')

    expect(message).toHaveTextContent('infra')
    expect(message).toHaveTextContent(/created in the workspace/i)
    expect(message).toHaveTextContent(/could not be added to this issue/i)

    // And the tag is not on the issue, because it is not.
    expect(
      screen.queryByRole('list', { name: 'Labels on this issue' }),
    ).not.toBeInTheDocument()
  })

  it('says only "added" when both halves worked', async () => {
    // The other side of the contract: the longer sentence must not appear on
    // the success path, or every created label reads as a half-failure.
    const view = renderPanel(
      <LabelsPanel workspaceSlug={WORKSPACE_SLUG} issueId={ISSUE_ID} />,
    )

    await view.link.resolve('IssueLabels', { data: issueLabelsData([]) })
    await view.link.resolve('WorkspaceLabels', { data: workspaceLabelsData([]) })

    await view.user.click(screen.getByRole('button', { name: 'Add' }))
    await view.user.click(screen.getByRole('menuitem', { name: 'New label' }))
    await view.user.type(
      screen.getByRole('textbox', { name: 'New label name' }),
      'infra',
    )
    await view.user.click(screen.getByRole('button', { name: 'Create' }))

    await view.link.resolve('LabelCreate', {
      data: {
        labelCreate: {
          __typename: 'LabelPayload' as const,
          label: label(3, { name: 'infra' }),
          errors: [],
        },
      },
    })

    await view.link.resolve('IssueLabelAttach', {
      data: {
        issueLabelAttach: {
          __typename: 'IssueLabelPayload' as const,
          issue: {
            __typename: 'Issue' as const,
            id: ISSUE_ID,
            labels: [label(3, { name: 'infra' })],
          },
          errors: [],
        },
      },
    })

    const status = await screen.findByRole('status')

    expect(status).toHaveTextContent('infra created and added')
    expect(status).not.toHaveTextContent(/could not be added/i)
  })
})
