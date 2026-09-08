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
  ReleaseDetailFieldsFragment,
  WorkspaceIntegrationsQuery,
} from '../../generated/operations'
import { commitRange, nextStatuses, shortSha } from './lib/releases'

/**
 * The releases screen, against the real router, cache and a controlled
 * network.
 *
 * ## The claims that matter most
 *
 * 1. **A status menu offers only the moves the server will accept.**
 *    `RELEASE_TRANSITIONS` lives in `app/domain/releases.py` and is not
 *    expressible in the schema -- `ReleaseStatusSetInput` takes any member of
 *    the enum -- so a screen that did not know the rule would offer "Mark
 *    deployed" on a failed release and teach the user the rule by refusing.
 * 2. **A release that was never deployed says "Never", not a dash.**
 *    `releases_deployed_at_matches_status` makes the status and the instant
 *    one fact, so a null there is a statement and not a missing value.
 * 3. **An id that does not resolve is said out loud.** A release carries an
 *    `environmentId` and a `repositoryId` and the schema exposes no object for
 *    either; a row that silently omitted the column would read as a release
 *    that went nowhere.
 */

const RELEASES_PATH = `/${WORKSPACE_SLUG}/releases`

const RELEASE_ID = '00000000-0000-4000-8000-0000000dd001'
const SECOND_RELEASE_ID = '00000000-0000-4000-8000-0000000dd002'
const ENVIRONMENT_ID = '00000000-0000-4000-8000-0000000ee101'
const ISSUE_ID = '00000000-0000-4000-8000-0000000aa901'

const COMMIT = 'a'.repeat(40)
const PREVIOUS_COMMIT = 'b'.repeat(40)

function releaseDetail(
  overrides: Partial<ReleaseDetailFieldsFragment> = {},
): ReleaseDetailFieldsFragment {
  return {
    __typename: 'Release',
    id: RELEASE_ID,
    name: 'v1.4.0',
    environmentId: ENVIRONMENT_ID,
    repositoryId: '90210',
    commitSha: COMMIT,
    previousCommitSha: PREVIOUS_COMMIT,
    status: 'PENDING',
    deployedAt: null,
    createdAt: '2026-09-01T09:30:00.000Z',
    updatedAt: '2026-09-01T09:30:00.000Z',
    notes: '## v1.4.0\n\n- Fixed the thing',
    issueIds: [ISSUE_ID],
    pullRequestNumbers: [84],
    ...overrides,
  }
}

function listRow(release: ReleaseDetailFieldsFragment) {
  return {
    __typename: 'Release' as const,
    id: release.id,
    name: release.name,
    environmentId: release.environmentId,
    repositoryId: release.repositoryId,
    commitSha: release.commitSha,
    previousCommitSha: release.previousCommitSha,
    status: release.status,
    deployedAt: release.deployedAt,
    createdAt: release.createdAt,
    updatedAt: release.updatedAt,
  }
}

const INTEGRATIONS: WorkspaceIntegrationsQuery = {
  githubIntegration: {
    __typename: 'GithubIntegration',
    status: 'CONNECTED',
    accountLogin: 'acme',
    connectedAt: '2026-01-01T00:00:00.000Z',
    repositories: [
      {
        __typename: 'GithubRepository',
        repositoryId: '90210',
        fullName: 'acme/vector',
        tracked: true,
      },
    ],
    // No team has turned a pull-request automation on, which is the default
    // and is all this screen needs from the field.
    automations: [],
  },
  slackIntegration: {
    __typename: 'SlackIntegration',
    status: 'DISCONNECTED',
    teamName: null,
    scopes: [],
  },
  teams: [],
}

async function openReleases(
  releases: readonly ReleaseDetailFieldsFragment[],
  {
    environments = [
      {
        __typename: 'Environment' as const,
        id: ENVIRONMENT_ID,
        name: 'Production',
        kind: 'PRODUCTION' as const,
        createdAt: '2026-01-01T00:00:00.000Z',
      },
    ],
  } = {},
) {
  const app = renderApp({ initialPath: RELEASES_PATH })

  await app.link.resolve('ReleaseList', {
    data: {
      releases: {
        __typename: 'ReleaseConnection',
        nodes: releases.map(listRow),
        pageInfo: { __typename: 'PageInfo', hasNextPage: false, endCursor: null },
      },
    },
  })

  await app.link.resolve('EnvironmentList', { data: { environments } })
  await app.link.resolve('WorkspaceIntegrations', { data: INTEGRATIONS })

  if (releases.length > 0) {
    await app.link.resolve('ReleaseDetail', { data: { release: releases[0] } })
  }

  return app
}

describe('the releases screen', () => {
  it('names the environment and the range on every row, and holds the notes in the panel', async () => {
    const app = await openReleases([releaseDetail()])

    const list = within(main()).getByRole('list', { name: 'Releases' })

    expect(within(list).getByText('v1.4.0')).toBeInTheDocument()
    expect(within(list).getByText('Production')).toBeInTheDocument()
    // The abbreviation, from the full SHAs. `bbbbbbb to aaaaaaa`.
    expect(
      within(list).getByText(`${shortSha(PREVIOUS_COMMIT)} to ${shortSha(COMMIT)}`),
    ).toBeInTheDocument()

    // The note is the panel's, never a row's: it can run to a hundred lines.
    expect(within(list).queryByText(/Fixed the thing/)).not.toBeInTheDocument()
    expect(screen.getByText(/Fixed the thing/)).toBeInTheDocument()

    app.unmount()
  })

  it('offers only the transitions the server will accept', async () => {
    const app = await openReleases([releaseDetail({ status: 'PENDING' })])

    // PENDING -> DEPLOYED | FAILED, and nothing else.
    expect(screen.getByRole('button', { name: 'Mark deployed' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Mark failed' })).toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: 'Mark rolled back' }),
    ).not.toBeInTheDocument()

    app.unmount()
  })

  it('offers no transition on a terminal release, and says why', async () => {
    const app = await openReleases([
      releaseDetail({
        status: 'FAILED',
        // A failed release never reached its target, so it carries no instant
        // -- `releases_deployed_at_matches_status` guarantees the pair.
        deployedAt: null,
      }),
    ])

    expect(screen.queryByRole('button', { name: /^Mark / })).not.toBeInTheDocument()
    expect(screen.getByText(/Failed is terminal/)).toBeInTheDocument()

    // And the absent deploy instant is a statement rather than a gap.
    expect(screen.getByText('Never')).toBeInTheDocument()

    app.unmount()
  })

  it('sends the status the menu offered and shows the refusal it comes back with', async () => {
    const app = await openReleases([releaseDetail({ status: 'PENDING' })])

    await app.user.click(screen.getByRole('button', { name: 'Mark deployed' }))

    const variables = await app.link.waitForRequest('ReleaseStatusSet')

    expect(variables['input']).toEqual({
      workspaceSlug: WORKSPACE_SLUG,
      id: RELEASE_ID,
      status: 'DEPLOYED',
    })

    // A refusal arrives inside `data`, over an HTTP 200, with a field named.
    // The screen must show it rather than treating the write as done.
    await app.link.resolve('ReleaseStatusSet', {
      data: {
        releaseStatusSet: {
          __typename: 'ReleasePayload',
          release: null,
          errors: [
            {
              __typename: 'ValidationErrorType',
              field: 'status',
              code: 'BAD_USER_INPUT',
              message: 'That release is no longer pending.',
            },
          ],
        },
      },
    })

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'That release is no longer pending.',
    )

    app.unmount()
  })

  it('says so when an environment or repository id does not resolve', async () => {
    const app = await openReleases([releaseDetail()], { environments: [] })

    // The row cannot name the target...
    expect(within(main()).getAllByText('Unknown environment').length).toBeGreaterThan(0)
    // ...and neither can the panel, which says which list came up empty.
    expect(screen.getByText('Not in this workspace’s list')).toBeInTheDocument()

    app.unmount()
  })

  it('refuses to open the composer without an environment to deploy to', async () => {
    const app = await openReleases([releaseDetail()], { environments: [] })

    await app.user.click(screen.getByRole('button', { name: 'Cut a release' }))

    expect(
      await screen.findByText(/This workspace has no environment to deploy to/),
    ).toBeInTheDocument()
    expect(screen.queryByLabelText('Version')).not.toBeInTheDocument()

    app.unmount()
  })

  it('sends a draft with a null previous commit when the field is left blank', async () => {
    const app = await openReleases([releaseDetail()])

    await app.user.click(screen.getByRole('button', { name: 'Cut a release' }))

    await app.user.type(await screen.findByLabelText('Version'), 'v1.5.0')
    await app.user.selectOptions(screen.getByLabelText('Environment'), ENVIRONMENT_ID)
    await app.user.selectOptions(screen.getByLabelText('Repository'), '90210')
    await app.user.type(screen.getByLabelText('Commit'), COMMIT)

    await app.user.click(screen.getByRole('button', { name: 'Cut release' }))

    const variables = await app.link.waitForRequest('ReleaseCreate')

    expect(variables['input']).toEqual({
      workspaceSlug: WORKSPACE_SLUG,
      name: 'v1.5.0',
      environmentId: ENVIRONMENT_ID,
      repositoryId: '90210',
      commitSha: COMMIT,
      // Null, not `''`: the server resolves the previous deploy itself, and an
      // empty string would be checked against the SHA format and refused.
      previousCommitSha: null,
    })

    app.unmount()
  })

  it('is navigable', async () => {
    const app = await openReleases([
      releaseDetail(),
      releaseDetail({
        id: SECOND_RELEASE_ID,
        name: 'v1.3.0',
        status: 'DEPLOYED',
        deployedAt: '2026-08-20T12:00:00.000Z',
        createdAt: '2026-08-20T09:00:00.000Z',
      }),
    ])

    expectOneFirstLevelHeading('Releases')
    expectHeadingLevelsUnbroken()
    expectEveryButtonNamed()
    expectNoDuplicateButtonNames()

    app.unmount()
  })
})

describe('the release transition table', () => {
  it('mirrors app/domain/releases.py, terminal states included', () => {
    expect(nextStatuses('PENDING')).toEqual(['DEPLOYED', 'FAILED'])
    expect(nextStatuses('DEPLOYED')).toEqual(['ROLLED_BACK'])
    expect(nextStatuses('FAILED')).toEqual([])
    expect(nextStatuses('ROLLED_BACK')).toEqual([])
  })
})

describe('the commit range', () => {
  it('says there is no lower bound rather than drawing a dash', () => {
    expect(
      commitRange({ commitSha: COMMIT, previousCommitSha: null }),
    ).toBe(`Everything up to ${shortSha(COMMIT)}`)
  })

  it('leaves anything that is not a full SHA alone', () => {
    // The database refuses to store an abbreviation, so a short string here
    // means something upstream is wrong -- trimming it to seven characters
    // would hide that.
    expect(shortSha('abc123')).toBe('abc123')
    expect(shortSha(COMMIT)).toBe('aaaaaaa')
  })
})
