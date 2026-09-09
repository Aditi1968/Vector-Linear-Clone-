import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { main, renderApp } from '../../test/render'
import { WORKSPACE_SLUG } from '../../test/factories'
import type { WorkspaceIntegrationsQuery } from '../../generated/operations'

/**
 * The settings screen, against the real router and a controlled network.
 *
 * The one thing here that is easy to get wrong and expensive to ship is
 * `UNCONFIGURED`. It means the *deployment* holds no credentials, and the
 * backend's start route answers 404 by design -- so a Connect button in that
 * state is an invitation to click something that cannot work, and the 404
 * reads as a bug in Vector rather than as a feature nobody enabled. These
 * tests pin the three states apart, and pin the Connect target to the
 * backend's own route rather than a provider URL.
 */

const PATH = `/${WORKSPACE_SLUG}/settings`

/**
 * What a Connect link should point at: the backend route, the workspace, and
 * an absolute `return_to` naming this screen.
 *
 * Built the way the component builds it rather than written out, so these
 * assertions stay about the CONTENTS of the URL and not about the order
 * URLSearchParams happens to serialise two keys in.
 */
function expectedStartHref(startPath: string): string {
  const parameters = new URLSearchParams({
    workspace: WORKSPACE_SLUG,
    return_to: new URL(PATH, window.location.origin).toString(),
  })

  return `${startPath}?${parameters.toString()}`
}

// A team and two of its states, for the pull-request automation controls.
// Ids rather than names everywhere they are compared, because the automation
// stores ids -- a team owns its state NAMES and may change them.
const TEAM_ID = '00000000-0000-7000-8000-0000000000d1'
// A second board, for the one test that needs two automations on screen at
// once -- which is the arrangement that used to produce duplicate control
// names.
const DESIGN_TEAM_ID = '00000000-0000-7000-8000-0000000000d2'
const IN_PROGRESS_STATE_ID = '00000000-0000-7000-8000-0000000000f2'
const DONE_STATE_ID = '00000000-0000-7000-8000-0000000000f4'

function integrations(
  github: WorkspaceIntegrationsQuery['githubIntegration']['status'],
  slack: WorkspaceIntegrationsQuery['slackIntegration']['status'],
): WorkspaceIntegrationsQuery {
  return {
    githubIntegration: {
      __typename: 'GithubIntegration',
      status: github,
      accountLogin: github === 'CONNECTED' ? 'acme-inc' : null,
      connectedAt: github === 'CONNECTED' ? '2026-01-01T00:00:00.000Z' : null,
      repositories:
        github === 'CONNECTED'
          ? [
              {
                __typename: 'GithubRepository',
                repositoryId: '1',
                fullName: 'acme-inc/vector',
                tracked: true,
              },
              {
                __typename: 'GithubRepository',
                repositoryId: '2',
                fullName: 'acme-inc/docs',
                // Untracked: the installation covers it and this workspace has
                // declined its deliveries. A second repository exists here so
                // the checkbox list has both states to render.
                tracked: false,
              },
            ]
          : [],
      automations: [],
    },
    slackIntegration: {
      __typename: 'SlackIntegration',
      status: slack,
      teamName: slack === 'CONNECTED' ? 'Acme HQ' : null,
      scopes: slack === 'CONNECTED' ? ['chat:write'] : [],
    },
    teams:
      github === 'CONNECTED'
        ? [
            {
              __typename: 'Team',
              id: TEAM_ID,
              key: 'ENG',
              name: 'Engineering',
              workflowStates: [
                {
                  __typename: 'WorkflowState',
                  id: IN_PROGRESS_STATE_ID,
                  name: 'In Progress',
                  category: 'STARTED',
                },
                {
                  __typename: 'WorkflowState',
                  id: DONE_STATE_ID,
                  name: 'Done',
                  category: 'COMPLETED',
                },
              ],
            },
          ]
        : [],
  }
}

async function open(
  github: WorkspaceIntegrationsQuery['githubIntegration']['status'],
  slack: WorkspaceIntegrationsQuery['slackIntegration']['status'],
) {
  const view = renderApp({ initialPath: PATH })

  await view.link.resolve('WorkspaceIntegrations', { data: integrations(github, slack) })

  return view
}

describe('workspace identity', () => {
  it('shows the slug from the URL and does not offer to edit it', async () => {
    await open('DISCONNECTED', 'DISCONNECTED')

    expect(within(main()).getByText(`/${WORKSPACE_SLUG}`)).toBeInTheDocument()
    // There is no `workspaceUpdate` mutation, so a form here would have
    // nowhere to submit. Said out loud rather than shown as disabled inputs,
    // which read as a permission problem.
    expect(
      within(main()).getByText(/The name and URL cannot be changed here/),
    ).toBeInTheDocument()
  })
})

describe('an unconfigured integration', () => {
  it('says it is unavailable and offers no Connect control', async () => {
    await open('UNCONFIGURED', 'UNCONFIGURED')

    expect(
      within(main()).getByText(
        /This Vector deployment has no GitHub credentials configured/,
      ),
    ).toBeInTheDocument()

    // The failure this test exists to prevent: a link to a route that 404s
    // by design.
    expect(screen.queryByRole('link', { name: 'Connect GitHub' })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Connect Slack' })).not.toBeInTheDocument()
  })
})

describe('an unconfirmed GitHub claim', () => {
  /**
   * `PENDING`: the workspace has claimed an installation and GitHub has not
   * confirmed it.
   *
   * The failure this pins is a fall-through. The panel used to branch on
   * CONNECTED, then DISCONNECTED, then everything else -- so a status added to
   * the schema would have landed in the `else` and told the user their
   * deployment has no GitHub credentials, which is a different and untrue
   * thing. Rendering it as CONNECTED would be worse: the whole reason the
   * server reports PENDING is that it does not believe the installation
   * belongs to this workspace yet.
   */
  it('says GitHub has not confirmed it, and claims no account', async () => {
    await open('PENDING', 'DISCONNECTED')

    expect(
      within(main()).getByText(/GitHub has not confirmed it yet/),
    ).toBeInTheDocument()

    // Not the unconfigured story, and not a connection.
    expect(
      within(main()).queryByText(
        /This Vector deployment has no GitHub credentials configured/,
      ),
    ).not.toBeInTheDocument()
    expect(screen.queryByText(/Connected to/)).not.toBeInTheDocument()
  })

  it('offers a way out in both directions', async () => {
    await open('PENDING', 'DISCONNECTED')

    // Re-running the install is the fix when the confirmation is simply late,
    // and it goes to the backend's own route like every other start.
    expect(
      screen.getByRole('link', { name: 'Run the GitHub installation again' }),
    ).toHaveAttribute('href', expectedStartHref('/github/install'))

    // Cancelling is the other way out: a claim nobody confirms should not have
    // to be waited out.
    expect(
      screen.getByRole('button', { name: 'Cancel GitHub claim' }),
    ).toBeInTheDocument()
  })
})

describe('a disconnected integration', () => {
  it('points Connect at the backend’s own route, never at the provider', async () => {
    await open('DISCONNECTED', 'DISCONNECTED')

    const github = screen.getByRole('link', { name: 'Connect GitHub' })
    const slack = screen.getByRole('link', { name: 'Connect Slack' })

    // The backend mints the CSRF `state` and redirects; a frontend-built
    // authorize URL would need a client id in this bundle and could not mint
    // a state the callback can check.
    expect(github).toHaveAttribute('href', expectedStartHref('/github/install'))
    expect(slack).toHaveAttribute('href', expectedStartHref('/slack/oauth/start'))

    for (const href of [github.getAttribute('href'), slack.getAttribute('href')]) {
      expect(href).not.toMatch(/github\.com|slack\.com|client_id/)
    }
  })

  it('tells the callback to come back to settings, not to the home page', async () => {
    /*
      THE P0, as the user met it: press Connect GitHub, complete the whole
      GitHub flow, and land on the Vector home page as though nothing had
      happened. The installation WAS recorded -- the flow was never broken.

      The link simply carried no `return_to`, so `allowed_redirect` on the
      server took its last branch and returned `allowlist[0]`: the first
      allowed ORIGIN. An origin has no path, so "where the deployment says
      people belong" and "the home page" were the same URL.

      So this asserts the parameter is present AND that it points at this
      screen. Asserting only its presence would pass for a link that sent
      everyone to the root explicitly.
    */
    await open('DISCONNECTED', 'DISCONNECTED')

    for (const name of ['Connect GitHub', 'Connect Slack']) {
      const href = screen.getByRole('link', { name }).getAttribute('href')
      const returnTo = new URLSearchParams(href!.split('?')[1]).get('return_to')

      expect(returnTo).not.toBeNull()

      // Absolute, because the server compares the ORIGIN against its
      // allowlist -- a bare path parses to no origin, is discarded as "not a
      // URL", and falls back to exactly the bug above.
      const parsed = new URL(returnTo!)

      expect(parsed.origin).toBe(window.location.origin)
      expect(parsed.pathname).toBe(`/${WORKSPACE_SLUG}/settings`)

      // The failure that started this: an origin with no path.
      expect(parsed.pathname).not.toBe('/')
    }
  })
})

describe('a connected integration', () => {
  it('names what it is connected to and confirms before disconnecting', async () => {
    const view = await open('CONNECTED', 'CONNECTED')

    expect(within(main()).getByText('Connected to acme-inc.')).toBeInTheDocument()
    expect(within(main()).getByText('acme-inc/vector')).toBeInTheDocument()
    // Scopes, because a connection granted fewer than Vector needs is a real
    // state that "Connected" alone would not distinguish.
    expect(within(main()).getByText('chat:write')).toBeInTheDocument()

    await view.user.click(screen.getByRole('button', { name: 'Disconnect GitHub' }))

    expect(view.link.countOf('WorkspaceGithubDisconnect')).toBe(0)
    expect(screen.getByRole('dialog')).toHaveAccessibleName('Disconnect GitHub?')

    await view.user.click(screen.getByRole('button', { name: 'Disconnect' }))

    await expect(
      view.link.waitForRequest('WorkspaceGithubDisconnect'),
    ).resolves.toMatchObject({ input: { workspaceSlug: WORKSPACE_SLUG } })
  })
})

describe('choosing which repositories are tracked', () => {
  it('shows a box per repository, ticked for the ones being applied', async () => {
    await open('CONNECTED', 'DISCONNECTED')

    expect(screen.getByRole('checkbox', { name: 'acme-inc/vector' })).toBeChecked()
    // Covered by the installation, declined here. The distinction is the whole
    // point of the setting: GitHub still grants it and Vector is not applying
    // its deliveries.
    expect(screen.getByRole('checkbox', { name: 'acme-inc/docs' })).not.toBeChecked()
  })

  it('sends the WHOLE set, not the one that changed', async () => {
    const view = await open('CONNECTED', 'DISCONNECTED')

    await view.user.click(screen.getByRole('checkbox', { name: 'acme-inc/docs' }))

    // Both ids, because two admins sending only their own change would
    // interleave into a selection neither of them chose.
    await expect(
      view.link.waitForRequest('WorkspaceGithubRepositoriesSet'),
    ).resolves.toMatchObject({
      input: { workspaceSlug: WORKSPACE_SLUG, repositoryIds: ['1', '2'] },
    })
  })

  it('untracking the last one sends an empty list rather than nothing', async () => {
    const view = await open('CONNECTED', 'DISCONNECTED')

    await view.user.click(screen.getByRole('checkbox', { name: 'acme-inc/vector' }))

    // A connected workspace that wants no development activity yet is a real
    // state; an omitted field would read as "the client forgot".
    await expect(
      view.link.waitForRequest('WorkspaceGithubRepositoriesSet'),
    ).resolves.toMatchObject({ input: { repositoryIds: [] } })
  })
})

describe('the pull request automation', () => {
  const AUTOMATION = 'Move ENG issues when a pull request that names them opens or merges'

  it('is off until somebody turns it on', async () => {
    await open('CONNECTED', 'DISCONNECTED')

    // The opt-in. An issue that changed status because somebody opened a pull
    // request, in a workspace that never asked for it, is a bug report.
    expect(screen.getByRole('checkbox', { name: AUTOMATION })).not.toBeChecked()
    expect(screen.queryByLabelText(/Pull request merged/)).not.toBeInTheDocument()
  })

  it('asks the server for the default rather than naming a state itself', async () => {
    const view = await open('CONNECTED', 'DISCONNECTED')

    await view.user.click(screen.getByRole('checkbox', { name: AUTOMATION }))

    // No state ids. There is no global "In Progress" for this screen to pick,
    // and the team here has states the server has to choose between by board
    // order -- so turning it on asks, and the answer comes back stored.
    await expect(
      view.link.waitForRequest('WorkspaceGithubAutomationSet'),
    ).resolves.toMatchObject({
      input: { workspaceSlug: WORKSPACE_SLUG, teamId: TEAM_ID, enabled: true },
    })
  })

  it('offers the configured states once a team has one, and sends ids', async () => {
    const view = renderApp({ initialPath: PATH })
    const data = integrations('CONNECTED', 'DISCONNECTED')

    await view.link.resolve('WorkspaceIntegrations', {
      data: {
        ...data,
        githubIntegration: {
          ...data.githubIntegration,
          automations: [
            {
              __typename: 'GithubIssueAutomation',
              teamId: TEAM_ID,
              startedStateId: IN_PROGRESS_STATE_ID,
              completedStateId: DONE_STATE_ID,
            },
          ],
        },
      },
    })

    expect(screen.getByRole('checkbox', { name: AUTOMATION })).toBeChecked()
    expect(screen.getByLabelText('Pull request merged — ENG')).toHaveValue(DONE_STATE_ID)

    // "Do nothing" is a real configuration: a team may automate the merge and
    // leave starting to whoever is doing the work.
    await view.user.selectOptions(
      screen.getByLabelText('Pull request opened — ENG'),
      '',
    )

    await expect(
      view.link.waitForRequest('WorkspaceGithubAutomationSet'),
    ).resolves.toMatchObject({
      input: {
        teamId: TEAM_ID,
        enabled: true,
        startedStateId: null,
        completedStateId: DONE_STATE_ID,
      },
    })
  })

  it('names every select for its own team, so two of them cannot collide', async () => {
    const view = renderApp({ initialPath: PATH })
    const data = integrations('CONNECTED', 'DISCONNECTED')
    const engineering = data.teams[0]!

    const automation = (teamId: string) => ({
      __typename: 'GithubIssueAutomation' as const,
      teamId,
      startedStateId: IN_PROGRESS_STATE_ID,
      completedStateId: DONE_STATE_ID,
    })

    await view.link.resolve('WorkspaceIntegrations', {
      data: {
        ...data,
        githubIntegration: {
          ...data.githubIntegration,
          automations: [automation(TEAM_ID), automation(DESIGN_TEAM_ID)],
        },
        teams: [
          engineering,
          { ...engineering, id: DESIGN_TEAM_ID, key: 'DES', name: 'Design' },
        ],
      },
    })

    // `getByLabelText` throws when two controls answer to one name, which is
    // the whole guard here: a second automated team used to add a second
    // select called "Pull request opened", and the legend that told them
    // apart is not read by anyone driving this by voice or by a rotor list.
    expect(screen.getByLabelText('Pull request opened — ENG')).toBeInTheDocument()
    expect(screen.getByLabelText('Pull request opened — DES')).toBeInTheDocument()
    expect(screen.getByLabelText('Pull request merged — DES')).toBeInTheDocument()
  })
})
