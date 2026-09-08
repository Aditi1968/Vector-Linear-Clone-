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
import type { EnvironmentFieldsFragment } from '../../generated/operations'

/**
 * The environments screen, against the real router, cache and a controlled
 * network.
 *
 * ## The claim that matters most
 *
 * "What is deployed to each" has no field of its own -- there is no
 * `Environment.releases` and no `latestRelease` -- so it is assembled from ONE
 * PAGE of `releases`. A target whose last deploy is older than that page shows
 * nothing, and the screen has to say "no deploy among the recent releases"
 * rather than "never deployed". The second sentence is a claim about the
 * workspace that this screen has no standing to make.
 *
 * The second claim: a release that was cut but never deployed is not what is
 * running anywhere. `deployedAt` is the axis, not `createdAt`.
 */

const ENVIRONMENTS_PATH = `/${WORKSPACE_SLUG}/environments`

const PRODUCTION_ID = '00000000-0000-4000-8000-0000000ee101'
const STAGING_ID = '00000000-0000-4000-8000-0000000ee102'

const PRODUCTION: EnvironmentFieldsFragment = {
  __typename: 'Environment',
  id: PRODUCTION_ID,
  name: 'Production',
  kind: 'PRODUCTION',
  createdAt: '2026-01-01T00:00:00.000Z',
}

const STAGING: EnvironmentFieldsFragment = {
  __typename: 'Environment',
  id: STAGING_ID,
  name: 'Staging',
  kind: 'STAGING',
  createdAt: '2026-01-01T00:00:00.000Z',
}

interface ReleaseRowOptions {
  id: string
  name: string
  environmentId: string
  status: 'PENDING' | 'DEPLOYED' | 'FAILED' | 'ROLLED_BACK'
  deployedAt: string | null
  createdAt: string
}

function releaseRow(options: ReleaseRowOptions) {
  return {
    __typename: 'Release' as const,
    id: options.id,
    name: options.name,
    environmentId: options.environmentId,
    repositoryId: '90210',
    commitSha: 'a'.repeat(40),
    previousCommitSha: null,
    status: options.status,
    deployedAt: options.deployedAt,
    createdAt: options.createdAt,
    updatedAt: options.createdAt,
  }
}

async function openEnvironments(
  environments: readonly EnvironmentFieldsFragment[],
  releases: readonly ReturnType<typeof releaseRow>[] = [],
) {
  const app = renderApp({ initialPath: ENVIRONMENTS_PATH })

  await app.link.resolve('EnvironmentList', { data: { environments } })

  await app.link.resolve('ReleaseList', {
    data: {
      releases: {
        __typename: 'ReleaseConnection',
        nodes: releases,
        pageInfo: { __typename: 'PageInfo', hasNextPage: false, endCursor: null },
      },
    },
  })

  return app
}

describe('the environments screen', () => {
  it('names what is running on each target from the releases in hand', async () => {
    const app = await openEnvironments(
      [PRODUCTION, STAGING],
      [
        releaseRow({
          id: '00000000-0000-4000-8000-0000000dd001',
          name: 'v1.4.0',
          environmentId: PRODUCTION_ID,
          status: 'DEPLOYED',
          deployedAt: '2026-09-01T10:00:00.000Z',
          createdAt: '2026-09-01T09:00:00.000Z',
        }),
        releaseRow({
          // Newer by `createdAt` and NOT deployed, so it is not what is
          // running. The screen must not pick it.
          id: '00000000-0000-4000-8000-0000000dd002',
          name: 'v1.5.0',
          environmentId: PRODUCTION_ID,
          status: 'PENDING',
          deployedAt: null,
          createdAt: '2026-09-05T09:00:00.000Z',
        }),
      ],
    )

    const list = within(main()).getByRole('list', { name: 'Environments' })
    const rows = within(list).getAllByRole('listitem')

    expect(within(rows[0] as HTMLElement).getByText('v1.4.0')).toBeInTheDocument()
    expect(within(rows[0] as HTMLElement).queryByText('v1.5.0')).not.toBeInTheDocument()

    // Staging has no deploy in the page, and says only that.
    expect(
      within(rows[1] as HTMLElement).getByText('No deploy among the recent releases'),
    ).toBeInTheDocument()
    expect(within(list).queryByText(/never deployed/i)).not.toBeInTheDocument()

    app.unmount()
  })

  it('shows a rolled-back release as what last reached the target', async () => {
    // Hiding it would make a target that was rolled back look like one nothing
    // ever went to, which is the opposite of what an incident review needs.
    const app = await openEnvironments(
      [PRODUCTION],
      [
        releaseRow({
          id: '00000000-0000-4000-8000-0000000dd003',
          name: 'v1.4.0',
          environmentId: PRODUCTION_ID,
          status: 'ROLLED_BACK',
          deployedAt: '2026-09-01T10:00:00.000Z',
          createdAt: '2026-09-01T09:00:00.000Z',
        }),
      ],
    )

    expect(screen.getByText('v1.4.0')).toBeInTheDocument()
    expect(screen.getByText('Rolled back')).toBeInTheDocument()

    app.unmount()
  })

  it('sends the kind the composer chose, which has no database default', async () => {
    const app = await openEnvironments([PRODUCTION])

    await app.user.click(screen.getByRole('button', { name: 'New environment' }))

    await app.user.type(await screen.findByLabelText('Name'), 'Preview EU')
    await app.user.selectOptions(screen.getByLabelText('Kind'), 'STAGING')

    // The trigger says "New environment" and the submit says "Add
    // environment": both are on the page while the dialog is open, so they
    // must not share an accessible name.
    expectNoDuplicateButtonNames()

    await app.user.click(screen.getByRole('button', { name: 'Add environment' }))

    const variables = await app.link.waitForRequest('EnvironmentCreate')

    expect(variables['input']).toEqual({
      workspaceSlug: WORKSPACE_SLUG,
      name: 'Preview EU',
      kind: 'STAGING',
    })

    app.unmount()
  })

  it('shows the duplicate-name refusal against the field that caused it', async () => {
    const app = await openEnvironments([PRODUCTION])

    await app.user.click(screen.getByRole('button', { name: 'New environment' }))
    await app.user.type(await screen.findByLabelText('Name'), 'Production')
    await app.user.click(screen.getByRole('button', { name: 'Add environment' }))

    await app.link.resolve('EnvironmentCreate', {
      data: {
        environmentCreate: {
          __typename: 'EnvironmentPayload',
          environment: null,
          errors: [
            {
              __typename: 'ValidationErrorType',
              field: 'name',
              code: 'BAD_USER_INPUT',
              message: 'An environment called Production already exists.',
            },
          ],
        },
      },
    })

    expect(
      await screen.findByText('An environment called Production already exists.'),
    ).toBeInTheDocument()

    app.unmount()
  })

  it('says that a target cannot be renamed or removed', async () => {
    // A ceiling of the API, not of this screen: `environmentCreate` is the
    // only environment mutation the schema has.
    const app = await openEnvironments([PRODUCTION])

    expect(
      screen.getByText(/An environment cannot be renamed or removed/),
    ).toBeInTheDocument()

    app.unmount()
  })

  it('is navigable', async () => {
    const app = await openEnvironments([PRODUCTION, STAGING])

    expectOneFirstLevelHeading('Environments')
    expectHeadingLevelsUnbroken()
    expectEveryButtonNamed()
    expectNoDuplicateButtonNames()

    app.unmount()
  })
})
