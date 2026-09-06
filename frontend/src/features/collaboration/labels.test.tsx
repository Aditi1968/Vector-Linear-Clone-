import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { LabelsPanel } from './index'
import {
  ISSUE_ID,
  issueLabelsData,
  label,
  labelsAttached,
  renderPanel,
  uuid,
  WORKSPACE_SLUG,
  workspaceLabelsData,
} from './testHarness'

/**
 * Labels on an issue.
 *
 * The claim under test is that attaching a label needs no hand-written cache
 * update at all: `issueLabelAttach` returns the whole `Issue` with its
 * `labels`, Apollo normalises that onto the entity the panel is watching, and
 * the tags re-render from the server's own answer. If that ever stops being
 * true the failure is a tag that does not appear until a reload, which is
 * exactly what these assert against.
 */

function tags(): string[] {
  const list = screen.queryByRole('list', { name: 'Labels on this issue' })

  return list === null
    ? []
    : within(list)
        .getAllByRole('listitem')
        .map((row) => row.textContent ?? '')
}

async function mount(
  attached: readonly ReturnType<typeof label>[] = [label(1, { name: 'bug' })],
  available: readonly ReturnType<typeof label>[] = [
    label(1, { name: 'bug' }),
    label(2, { name: 'design' }),
  ],
) {
  const view = renderPanel(
    <LabelsPanel workspaceSlug={WORKSPACE_SLUG} issueId={ISSUE_ID} />,
  )

  await view.link.resolve('IssueLabels', { data: issueLabelsData(attached) })
  await view.link.resolve('WorkspaceLabels', { data: workspaceLabelsData(available) })

  return view
}

describe('labels on an issue', () => {
  it('says there are none rather than showing an empty row', async () => {
    await mount([])

    expect(screen.getByText('No labels')).toBeInTheDocument()
    expect(tags()).toEqual([])
  })

  it('offers only the labels not already attached', async () => {
    const view = await mount()

    await view.user.click(screen.getByRole('button', { name: 'Add' }))

    const menu = screen.getByRole('menu', { name: 'Add a label' })
    const items = within(menu)
      .getAllByRole('menuitem')
      .map((item) => item.textContent)

    expect(items).toContain('design')
    // Already on the issue. Offering it would be offering a mutation the
    // server would refuse.
    expect(items).not.toContain('bug')
  })

  it('shows an attached label from the mutation payload, with no refetch', async () => {
    const view = await mount()

    await view.user.click(screen.getByRole('button', { name: 'Add' }))
    await view.user.click(screen.getByRole('menuitem', { name: 'design' }))

    expect(await view.link.waitForRequest('IssueLabelAttach')).toEqual({
      input: {
        workspaceSlug: WORKSPACE_SLUG,
        issueId: ISSUE_ID,
        labelId: uuid(2),
      },
    })

    await view.link.resolve('IssueLabelAttach', {
      data: labelsAttached([label(1, { name: 'bug' }), label(2, { name: 'design' })]),
    })

    expect(tags()).toHaveLength(2)
    expect(tags()[1]).toContain('design')
    // Normalisation did the work; nothing asked the server again.
    expect(view.link.countOf('IssueLabels')).toBe(1)
    expect(
      within(screen.getByRole('region', { name: /Labels/ })).getByRole('status'),
    ).toHaveTextContent('design added')
  })

  it('removes a label through the tag own remove control', async () => {
    const view = await mount()

    await view.user.click(screen.getByRole('button', { name: 'Remove bug' }))

    expect(await view.link.waitForRequest('IssueLabelDetach')).toEqual({
      input: { workspaceSlug: WORKSPACE_SLUG, issueId: ISSUE_ID, labelId: uuid(1) },
    })

    await view.link.resolve('IssueLabelDetach', {
      data: {
        issueLabelDetach: {
          __typename: 'IssueLabelPayload',
          issue: { __typename: 'Issue', id: ISSUE_ID, labels: [] },
          errors: [],
        },
      },
    })

    expect(tags()).toEqual([])
  })

  it('creates a label and attaches it, in that order, as two mutations', async () => {
    const view = await mount()

    await view.user.click(screen.getByRole('button', { name: 'Add' }))
    await view.user.click(screen.getByRole('menuitem', { name: 'New label' }))

    await view.user.type(
      screen.getByRole('textbox', { name: 'New label name' }),
      'infra',
    )
    await view.user.click(screen.getByRole('button', { name: 'Create' }))

    // `color` is deliberately absent: the input declares a server default and
    // choosing a hex here would be the panel deciding something the product
    // has not.
    expect(await view.link.waitForRequest('LabelCreate')).toEqual({
      input: { workspaceSlug: WORKSPACE_SLUG, name: 'infra' },
    })

    await view.link.resolve('LabelCreate', {
      data: {
        labelCreate: {
          __typename: 'LabelPayload' as const,
          label: label(3, { name: 'infra' }),
          errors: [],
        },
      },
    })

    // The schema has no create-and-attach, so the attach is a second request
    // carrying the id the server just assigned.
    expect(await view.link.waitForRequest('IssueLabelAttach')).toEqual({
      input: { workspaceSlug: WORKSPACE_SLUG, issueId: ISSUE_ID, labelId: uuid(3) },
    })

    await view.link.resolve('IssueLabelAttach', {
      data: labelsAttached([label(1, { name: 'bug' }), label(3, { name: 'infra' })]),
    })

    expect(tags()[1]).toContain('infra')
  })

  it('reports a rejected label name and keeps the form open', async () => {
    const view = await mount()

    await view.user.click(screen.getByRole('button', { name: 'Add' }))
    await view.user.click(screen.getByRole('menuitem', { name: 'New label' }))
    await view.user.type(screen.getByRole('textbox', { name: 'New label name' }), 'bug')
    await view.user.click(screen.getByRole('button', { name: 'Create' }))

    await view.link.resolve('LabelCreate', {
      data: {
        labelCreate: {
          __typename: 'LabelPayload' as const,
          label: null,
          errors: [
            {
              __typename: 'ValidationErrorType' as const,
              field: 'name',
              code: 'TAKEN',
              message: 'A label with that name already exists.',
            },
          ],
        },
      },
    })

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'A label with that name already exists.',
    )
    expect(screen.getByRole('textbox', { name: 'New label name' })).toHaveValue('bug')
    expect(view.link.countOf('IssueLabelAttach')).toBe(0)
  })

  it('offers the next page of workspace labels rather than pretending there is none', async () => {
    const view = renderPanel(
      <LabelsPanel workspaceSlug={WORKSPACE_SLUG} issueId={ISSUE_ID} />,
    )

    await view.link.resolve('IssueLabels', { data: issueLabelsData([]) })
    await view.link.resolve('WorkspaceLabels', {
      data: workspaceLabelsData([label(2, { name: 'design' })], {
        hasNextPage: true,
        endCursor: 'cursor-one',
      }),
    })

    await view.user.click(screen.getByRole('button', { name: 'Add' }))
    await view.user.click(screen.getByRole('menuitem', { name: 'Show more labels' }))

    expect(await view.link.waitForRequest('WorkspaceLabels')).toMatchObject({
      after: 'cursor-one',
    })

    await view.link.resolve('WorkspaceLabels', {
      data: workspaceLabelsData([label(4, { name: 'infra' })]),
    })

    await view.user.click(screen.getByRole('button', { name: 'Add' }))

    const items = within(screen.getByRole('menu', { name: 'Add a label' }))
      .getAllByRole('menuitem')
      .map((item) => item.textContent)

    // Both pages, once each: the field policy merged them and deduped.
    expect(items).toContain('design')
    expect(items).toContain('infra')
  })
})
