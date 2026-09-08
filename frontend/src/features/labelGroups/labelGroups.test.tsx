import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import {
  expectEveryButtonNamed,
  expectHeadingLevelsUnbroken,
  expectNoDuplicateButtonNames,
  expectOneFirstLevelHeading,
} from '../../test/a11y'
import { main, renderApp } from '../../test/render'
import { WORKSPACE_SLUG } from '../../test/factories'
import type {
  GroupedLabelFieldsFragment,
  LabelGroupFieldsFragment,
} from '../../generated/operations'
import { labelsByGroup, ungroupedLabels } from './lib/labelGroups'

/**
 * The label-groups screen, against the real router, cache and a controlled
 * network.
 *
 * ## The claim that matters most
 *
 * Exclusivity is the whole feature, and it is enforced by PostgreSQL rather
 * than by this server -- migration 021 carries the flag down onto each label
 * and refuses a second attachment through a partial unique index. Two
 * consequences the screen must render honestly:
 *
 *   1. **Turning exclusivity on can be refused**, for a group whose labels
 *      already share an issue. That refusal arrives as `rejected` inside
 *      `data`, over an HTTP 200, and it is the message a person needs.
 *      Swallowing it would leave the checkbox looking as if it had taken.
 *   2. **An edit sends both fields.** `LabelGroupUpdateInput` declares
 *      `name: String!` and `exclusive: Boolean!` -- a whole-row replace, not a
 *      patch -- so a form that sent only the field somebody touched would
 *      blank the other.
 *
 * And the third, which is about honesty rather than the rule: membership is
 * read from one page of 50 labels, so an apparently empty group is only
 * "empty among the labels loaded".
 */

const LABEL_GROUPS_PATH = `/${WORKSPACE_SLUG}/label-groups`

const PRIORITY_GROUP_ID = '00000000-0000-4000-8000-0000000ab001'
const PLATFORM_GROUP_ID = '00000000-0000-4000-8000-0000000ab002'
const URGENT_LABEL_ID = '00000000-0000-4000-8000-0000000ac001'
const LATER_LABEL_ID = '00000000-0000-4000-8000-0000000ac002'
const LOOSE_LABEL_ID = '00000000-0000-4000-8000-0000000ac003'

const PRIORITY_GROUP: LabelGroupFieldsFragment = {
  __typename: 'LabelGroup',
  id: PRIORITY_GROUP_ID,
  name: 'Priority',
  exclusive: true,
  createdAt: '2026-01-01T00:00:00.000Z',
  updatedAt: '2026-01-01T00:00:00.000Z',
}

const PLATFORM_GROUP: LabelGroupFieldsFragment = {
  __typename: 'LabelGroup',
  id: PLATFORM_GROUP_ID,
  name: 'Platform',
  exclusive: false,
  createdAt: '2026-01-01T00:00:00.000Z',
  updatedAt: '2026-01-01T00:00:00.000Z',
}

function label(
  id: string,
  name: string,
  groupId: string | null,
): GroupedLabelFieldsFragment {
  return { __typename: 'Label', id, name, color: '#0a7189', groupId }
}

const LABELS = [
  label(URGENT_LABEL_ID, 'Urgent', PRIORITY_GROUP_ID),
  label(LATER_LABEL_ID, 'Later', PRIORITY_GROUP_ID),
  label(LOOSE_LABEL_ID, 'Flaky', null),
]

async function openLabelGroups(
  groups: readonly LabelGroupFieldsFragment[] = [PRIORITY_GROUP, PLATFORM_GROUP],
  labels: readonly GroupedLabelFieldsFragment[] = LABELS,
  { hasNextPage = false }: { hasNextPage?: boolean } = {},
) {
  const app = renderApp({ initialPath: LABEL_GROUPS_PATH })

  await app.link.resolve('LabelGroupList', { data: { labelGroups: groups } })

  await app.link.resolve('GroupedLabelList', {
    data: {
      labels: {
        __typename: 'LabelConnection',
        nodes: labels,
        pageInfo: { __typename: 'PageInfo', hasNextPage },
      },
    },
  })

  return app
}

describe('the label groups screen', () => {
  it('says which groups are exclusive and what that means', async () => {
    const app = await openLabelGroups()

    const list = within(main()).getByRole('list', { name: 'Label groups' })

    // The rule is in the words, not only in the badge tone: a coloured pill is
    // not information to somebody who cannot separate the hues.
    expect(within(list).getByText('One at a time')).toBeInTheDocument()
    expect(within(list).getByText('Any number')).toBeInTheDocument()

    expect(
      screen.getByText(/An issue may wear at most one label from this group/),
    ).toBeInTheDocument()

    app.unmount()
  })

  it('shows a group holding the labels that name it', async () => {
    const app = await openLabelGroups()

    // Priority is selected by default, and holds two of the three labels.
    expect(screen.getByRole('button', { name: 'Remove Urgent' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Remove Later' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Remove Flaky' })).not.toBeInTheDocument()

    app.unmount()
  })

  it('offers only ungrouped labels in the picker', async () => {
    const app = await openLabelGroups()

    const picker = screen.getByLabelText('Ungrouped label')

    expect(within(picker).getByRole('option', { name: 'Flaky' })).toBeInTheDocument()
    // Moving a label straight out of another group would empty a group the
    // person was not looking at, from a menu that never named it.
    expect(within(picker).queryByRole('option', { name: 'Urgent' })).not.toBeInTheDocument()

    app.unmount()
  })

  it('sends both fields on an edit, because the input is a whole-row replace', async () => {
    const app = await openLabelGroups()

    await app.user.click(screen.getByRole('button', { name: 'Edit group…' }))

    const nameField = await screen.findByLabelText('Group name')

    await app.user.clear(nameField)
    await app.user.type(nameField, 'Severity')

    await app.user.click(screen.getByRole('button', { name: 'Save changes' }))

    const variables = await app.link.waitForRequest('LabelGroupUpdate')

    expect(variables['input']).toEqual({
      workspaceSlug: WORKSPACE_SLUG,
      id: PRIORITY_GROUP_ID,
      name: 'Severity',
      // Untouched, and sent anyway. Omitting it is not expressible: the field
      // is `Boolean!` and leaving it out would be a document the server
      // refuses at variable coercion.
      exclusive: true,
    })

    app.unmount()
  })

  it('shows the database’s refusal when exclusivity cannot be turned on', async () => {
    const app = await openLabelGroups([PLATFORM_GROUP], [label(LOOSE_LABEL_ID, 'Flaky', null)])

    await app.user.click(screen.getByRole('button', { name: 'Edit group…' }))

    await app.user.click(await screen.findByLabelText('One label at a time'))
    await app.user.click(screen.getByRole('button', { name: 'Save changes' }))

    // The cascade reaches `issue_labels_exclusive_group_key` and the whole
    // UPDATE fails. That failure is the feature, per migration 021, and it has
    // to reach the person who asked for it.
    await app.link.resolve('LabelGroupUpdate', {
      data: {
        labelGroupUpdate: {
          __typename: 'LabelGroupPayload',
          group: null,
          errors: [
            {
              __typename: 'ValidationErrorType',
              field: 'exclusive',
              code: 'BAD_USER_INPUT',
              message: 'Some issues already wear two labels from this group.',
            },
          ],
        },
      },
    })

    expect(
      await screen.findByText('Some issues already wear two labels from this group.'),
    ).toBeInTheDocument()

    app.unmount()
  })

  it('moves a label into the selected group', async () => {
    const app = await openLabelGroups()

    await app.user.selectOptions(screen.getByLabelText('Ungrouped label'), LOOSE_LABEL_ID)
    await app.user.click(screen.getByRole('button', { name: 'Add to group' }))

    const variables = await app.link.waitForRequest('LabelSetGroup')

    expect(variables['input']).toEqual({
      workspaceSlug: WORKSPACE_SLUG,
      labelId: LOOSE_LABEL_ID,
      groupId: PRIORITY_GROUP_ID,
    })

    app.unmount()
  })

  it('qualifies an empty group when more labels are outstanding', async () => {
    const app = await openLabelGroups([PLATFORM_GROUP], [], { hasNextPage: true })

    expect(
      screen.getByText(/None among the labels loaded/),
    ).toBeInTheDocument()
    expect(screen.queryByText(/None yet\./)).not.toBeInTheDocument()

    app.unmount()
  })

  it('says an empty group is empty when every label is in hand', async () => {
    const app = await openLabelGroups([PLATFORM_GROUP], [])

    expect(screen.getByText(/None yet\./)).toBeInTheDocument()

    app.unmount()
  })

  it('is navigable', async () => {
    const app = await openLabelGroups()

    expectOneFirstLevelHeading('Label groups')
    expectHeadingLevelsUnbroken()
    expectEveryButtonNamed()
    expectNoDuplicateButtonNames()

    app.unmount()
  })
})

describe('assembling membership from the labels', () => {
  it('groups labels by the group they name and leaves the rest ungrouped', () => {
    // `LabelGroup` publishes no member list -- migration 021 puts membership
    // on the LABEL, because the generated exclusivity key has to be computable
    // from the label's own row -- so this is the only place the question can
    // be answered from.
    const byGroup = labelsByGroup(LABELS)

    expect(byGroup.get(PRIORITY_GROUP_ID)?.map((one) => one.name)).toEqual([
      'Urgent',
      'Later',
    ])
    expect(byGroup.has(PLATFORM_GROUP_ID)).toBe(false)
    expect(ungroupedLabels(LABELS).map((one) => one.name)).toEqual(['Flaky'])
  })
})
