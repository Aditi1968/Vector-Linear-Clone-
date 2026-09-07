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
              },
            ]
          : [],
    },
    slackIntegration: {
      __typename: 'SlackIntegration',
      status: slack,
      teamName: slack === 'CONNECTED' ? 'Acme HQ' : null,
      scopes: slack === 'CONNECTED' ? ['chat:write'] : [],
    },
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

describe('a disconnected integration', () => {
  it('points Connect at the backend’s own route, never at the provider', async () => {
    await open('DISCONNECTED', 'DISCONNECTED')

    const github = screen.getByRole('link', { name: 'Connect GitHub' })
    const slack = screen.getByRole('link', { name: 'Connect Slack' })

    // The backend mints the CSRF `state` and redirects; a frontend-built
    // authorize URL would need a client id in this bundle and could not mint
    // a state the callback can check.
    expect(github).toHaveAttribute(
      'href',
      `/github/install?workspace=${WORKSPACE_SLUG}`,
    )
    expect(slack).toHaveAttribute(
      'href',
      `/slack/oauth/start?workspace=${WORKSPACE_SLUG}`,
    )

    for (const href of [github.getAttribute('href'), slack.getAttribute('href')]) {
      expect(href).not.toMatch(/github\.com|slack\.com|client_id/)
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
