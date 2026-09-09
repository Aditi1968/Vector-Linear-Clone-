import { describe, expect, it } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { RouterProvider, createMemoryRouter } from 'react-router-dom'
import type { RouteObject } from 'react-router-dom'

import { AppProviders } from '../../app/providers/AppProviders'
import { ControlledLink } from '../../test/controlledLink'
import { createTestClient } from '../../test/client'
import { onboardingRoutes } from '.'

/**
 * What can actually break in first-run setup.
 *
 * Four things, and they are the four that would be shipped broken without a
 * test because each of them looks fine in the happy path someone clicks
 * through by hand:
 *
 *   1. A refresh mid-setup restarting from the beginning. The whole reason
 *      progress is derived rather than stored is to make this impossible, so
 *      it is the claim worth pinning.
 *   2. An existing user being marched through setup again.
 *   3. A slug the server refuses being reported somewhere nobody sees it.
 *   4. An integration the deployment has no credentials for being offered as
 *      a button that cannot work.
 *
 * Everything below the link is production code: the real route table for this
 * feature, the real guard, the real cache. Only the network is replaced.
 */

const WORKSPACE = {
  __typename: 'Workspace' as const,
  id: '00000000-0000-4000-8000-0000000000w1'.replace('w', 'a'),
  slug: 'acme',
  name: 'Acme',
}

const MEMBERSHIP = {
  __typename: 'WorkspaceMembership' as const,
  workspace: WORKSPACE,
  role: 'OWNER' as const,
  createdAt: '2026-01-15T12:00:00.000Z',
}

const TEAM = {
  __typename: 'Team' as const,
  id: '00000000-0000-4000-8000-0000000000e1'.replace('e', 'b'),
  key: 'ENG',
  name: 'Engineering',
}

/**
 * Mount this feature's routes over a controllable network.
 *
 * A local helper rather than `src/test/render.tsx`, which mounts the
 * application's own route table -- and that table does not include
 * `onboardingRoutes` yet, because composing it belongs to whoever owns
 * `app/routes`. The catch-all is what lets a redirect *out* of onboarding
 * resolve to something instead of throwing "no route matches".
 */
function renderOnboarding(initialPath: string) {
  const link = new ControlledLink()
  const client = createTestClient(link)

  const routes: RouteObject[] = [
    ...onboardingRoutes,
    { path: '*', element: <div data-testid="elsewhere" /> },
  ]

  const router = createMemoryRouter(routes, { initialEntries: [initialPath] })

  render(
    <AppProviders client={client}>
      <RouterProvider router={router} />
    </AppProviders>,
  )

  return {
    link,
    user: userEvent.setup(),
    currentPath: () => router.state.location.pathname,
  }
}

/** Answer the progress queries. `teams` is skipped when there is no workspace. */
async function answerProgress(
  link: ControlledLink,
  options: { workspace: boolean; team: boolean },
) {
  await link.resolve('OnboardingWorkspaces', {
    data: { myWorkspaces: options.workspace ? [MEMBERSHIP] : [] },
  })

  if (options.workspace) {
    await link.resolve('OnboardingTeams', {
      data: { teams: options.team ? [TEAM] : [] },
    })
  }
}

describe('resuming from derived state', () => {
  it('sends a refresh on the workspace step forward when a workspace exists', async () => {
    // The refresh case exactly: the browser is asking for the step that was
    // on screen before the reload, and the server already has the workspace
    // that step created. Nothing remembers that; it is re-derived.
    const { link, currentPath } = renderOnboarding('/onboarding/workspace')

    await answerProgress(link, { workspace: true, team: false })

    await waitFor(() => {
      expect(currentPath()).toBe('/onboarding/team')
    })
    expect(
      screen.getByRole('heading', { name: 'Create your first team' }),
    ).toBeInTheDocument()
  })

  it('keeps a refresh on the team step there when the team is still missing', async () => {
    const { link, currentPath } = renderOnboarding('/onboarding/team')

    await answerProgress(link, { workspace: true, team: false })

    await waitFor(() => {
      expect(
        screen.getByRole('heading', { name: 'Create your first team' }),
      ).toBeInTheDocument()
    })
    expect(currentPath()).toBe('/onboarding/team')
  })

  it('sends a refresh on the team step back when there is no workspace yet', async () => {
    // The other direction, which is the one that would let a step run a query
    // for a workspace that does not exist.
    const { link, currentPath } = renderOnboarding('/onboarding/team')

    await answerProgress(link, { workspace: false, team: false })

    await waitFor(() => {
      expect(currentPath()).toBe('/onboarding/workspace')
    })
  })

  it('resumes at the team step from /onboarding itself', async () => {
    const { link, currentPath } = renderOnboarding('/onboarding')

    await answerProgress(link, { workspace: true, team: false })

    await waitFor(() => {
      expect(currentPath()).toBe('/onboarding/team')
    })
  })
})

describe('an account that is already set up', () => {
  it('never sees setup again from /onboarding', async () => {
    const { link, currentPath } = renderOnboarding('/onboarding')

    await answerProgress(link, { workspace: true, team: true })

    await waitFor(() => {
      // Into the workspace, not into a step -- and workspace-scoped, which is
      // the shape every product URL has now.
      expect(currentPath()).toBe('/acme/issues')
    })
    expect(screen.queryByRole('heading', { name: /Create your/ })).toBeNull()
  })

  it('does not offer the workspace step a second time', async () => {
    const { link, currentPath } = renderOnboarding('/onboarding/workspace')

    await answerProgress(link, { workspace: true, team: true })

    await waitFor(() => {
      expect(currentPath()).not.toBe('/onboarding/workspace')
    })
    expect(screen.queryByLabelText('Workspace name')).toBeNull()
  })
})

describe('a slug the server refuses', () => {
  it('reports SLUG_TAKEN on the slug field, with focus, and keeps the form', async () => {
    const { link, user, currentPath } = renderOnboarding('/onboarding/workspace')

    await answerProgress(link, { workspace: false, team: false })

    const nameInput = await screen.findByLabelText('Workspace name')
    await user.type(nameInput, 'Acme')

    const slugInput = screen.getByLabelText('Workspace URL')
    // Derived from the name until edited by hand.
    expect(slugInput).toHaveValue('acme')

    await user.click(screen.getByRole('button', { name: 'Create workspace' }))

    const sent = await link.waitForRequest('OnboardingWorkspaceCreate')
    expect(sent['input']).toEqual({ name: 'Acme', slug: 'acme' })

    await link.resolve('OnboardingWorkspaceCreate', {
      data: {
        workspaceCreate: {
          __typename: 'WorkspacePayload',
          workspace: null,
          errors: [
            {
              __typename: 'ValidationErrorType',
              field: 'slug',
              code: 'SLUG_TAKEN',
              message: 'That workspace URL is already taken.',
            },
          ],
        },
      },
    })

    const message = await screen.findByText('That workspace URL is already taken.')

    // Reported *on the field*, not only in a banner: announced with the
    // label, and focused, so a keyboard user is on the control to change.
    expect(slugInput).toHaveAttribute('aria-invalid', 'true')
    expect(slugInput.getAttribute('aria-describedby')).toContain(message.id)
    expect(slugInput).toHaveFocus()

    // Nothing lost, and nowhere else. A redirect here would discard the name
    // as well as the message.
    expect(nameInput).toHaveValue('Acme')
    expect(slugInput).toHaveValue('acme')
    expect(currentPath()).toBe('/onboarding/workspace')
  })

  it('states the slug rule before anything is submitted', async () => {
    const { link } = renderOnboarding('/onboarding/workspace')

    await answerProgress(link, { workspace: false, team: false })

    const slugInput = await screen.findByLabelText('Workspace URL')

    expect(slugInput).toHaveAccessibleDescription(
      /Lowercase letters, digits and hyphens/,
    )
  })
})

describe('integrations the deployment cannot offer', () => {
  async function renderIntegrations(statuses: {
    // Only GitHub has PENDING: it is the one install whose callback hands the
    // server an installation id it cannot verify on the spot.
    github: 'UNCONFIGURED' | 'DISCONNECTED' | 'PENDING' | 'CONNECTED'
    slack: 'UNCONFIGURED' | 'DISCONNECTED' | 'CONNECTED'
  }) {
    const view = renderOnboarding('/onboarding/integrations')

    await answerProgress(view.link, { workspace: true, team: true })
    await view.link.resolve('OnboardingIntegrations', {
      data: {
        githubIntegration: {
          __typename: 'GithubIntegration',
          status: statuses.github,
          accountLogin: statuses.github === 'CONNECTED' ? 'acme-inc' : null,
        },
        slackIntegration: {
          __typename: 'SlackIntegration',
          status: statuses.slack,
          teamName: statuses.slack === 'CONNECTED' ? 'Acme HQ' : null,
        },
      },
    })

    return view
  }

  it('says unavailable and offers no button when the server says UNCONFIGURED', async () => {
    await renderIntegrations({ github: 'UNCONFIGURED', slack: 'UNCONFIGURED' })

    expect(
      await screen.findByText(/no GitHub credentials configured/),
    ).toBeInTheDocument()
    expect(screen.getByText(/no Slack credentials configured/)).toBeInTheDocument()

    // The point of the whole card: a Connect control here would 404 by
    // design, and the 404 would read as a bug in Vector.
    expect(screen.queryByRole('link', { name: /Connect GitHub/ })).toBeNull()
    expect(screen.queryByRole('link', { name: /Connect Slack/ })).toBeNull()
  })

  it('points a DISCONNECTED provider at the backend start route', async () => {
    await renderIntegrations({ github: 'DISCONNECTED', slack: 'DISCONNECTED' })

    // Awaited so the query below runs against a settled render.
    await screen.findByRole('link', { name: 'Connect GitHub' })

    // The backend's own endpoint, which mints the CSRF state and redirects.
    // Never a github.com URL assembled here, and no client id anywhere.
    for (const [name, startPath] of [
      ['Connect GitHub', '/github/install'],
      ['Connect Slack', '/slack/oauth/start'],
    ]) {
      const href = screen.getByRole('link', { name }).getAttribute('href')

      expect(href).toMatch(new RegExp(`^${startPath}\?`))
      expect(new URLSearchParams(href!.split('?')[1]).get('workspace')).toBe('acme')
      expect(href).not.toMatch(/github\.com|slack\.com|client_id/)
    }
  })

  it('sends the callback back to this step, not to the workspace home', async () => {
    /*
      The same defect the settings screen had, and this is the call site where
      getting it wrong is worst: a person part-way through onboarding who
      connects GitHub and is dropped on the home page has lost their place in
      a flow they had not finished.

      Each call site names its own return path -- settings returns to
      settings, this returns here -- so neither infers the other's, and the
      onboarding path cannot leak into the settings flow.
    */
    await renderIntegrations({ github: 'DISCONNECTED', slack: 'DISCONNECTED' })

    const github = await screen.findByRole('link', { name: 'Connect GitHub' })
    const href = github.getAttribute('href')
    const returnTo = new URLSearchParams(href!.split('?')[1]).get('return_to')

    expect(returnTo).not.toBeNull()

    // Absolute, because the server matches the ORIGIN against its allowlist
    // and a bare path parses to no origin at all.
    const parsed = new URL(returnTo!)

    expect(parsed.origin).toBe(window.location.origin)
    expect(parsed.pathname).toBe('/onboarding/integrations')
    expect(parsed.pathname).not.toBe('/')
  })

  it('does not read an unconfirmed GitHub claim as a connection', async () => {
    // The card branches CONNECTED, PENDING, DISCONNECTED, else -- and the
    // `else` is the unconfigured story. A status without a branch of its own
    // would land there and tell the person their deployment has no GitHub
    // credentials, which is untrue; reading it as CONNECTED would be worse,
    // since the server reports PENDING precisely because it does not yet
    // believe the installation is this workspace's.
    await renderIntegrations({ github: 'PENDING', slack: 'DISCONNECTED' })

    expect(
      await screen.findByText(/GitHub has not confirmed this installation yet/),
    ).toBeInTheDocument()
    expect(screen.queryByText(/no GitHub credentials configured/)).toBeNull()
    expect(screen.queryByRole('link', { name: 'Connect GitHub' })).toBeNull()
  })

  it('is always skippable into the workspace', async () => {
    const { currentPath, user } = await renderIntegrations({
      github: 'UNCONFIGURED',
      slack: 'UNCONFIGURED',
    })

    await user.click(await screen.findByRole('link', { name: /Finish/ }))

    await waitFor(() => {
      expect(currentPath()).toBe('/acme/issues')
    })
  })
})

describe('finishing the team step', () => {
  it('advances to the invite step rather than into the product', async () => {
    // The redirect race: creating the team makes setup "complete", and a
    // guard that ejected on that would skip the two optional steps entirely.
    const { link, user, currentPath } = renderOnboarding('/onboarding/team')

    await answerProgress(link, { workspace: true, team: false })

    await user.type(await screen.findByLabelText('Team name'), 'Engineering')
    expect(screen.getByLabelText('Issue key')).toHaveValue('ENG')

    await user.click(screen.getByRole('button', { name: 'Create team' }))

    const sent = await link.waitForRequest('OnboardingTeamCreate')
    expect(sent['input']).toEqual({
      workspaceSlug: 'acme',
      name: 'Engineering',
      key: 'ENG',
    })

    await link.resolve('OnboardingTeamCreate', {
      data: {
        teamCreate: {
          __typename: 'TeamPayload',
          team: TEAM,
          errors: [],
        },
      },
    })
    // The refetch `awaitRefetchQueries` waits for, answered as the server now
    // would: the team exists.
    await link.resolve('OnboardingTeams', { data: { teams: [TEAM] } })

    await waitFor(() => {
      expect(currentPath()).toBe('/onboarding/invite')
    })
  })
})

describe('the invite step', () => {
  it('shows the link once and says Vector will not send it', async () => {
    const { link, user } = renderOnboarding('/onboarding/invite')

    await answerProgress(link, { workspace: true, team: true })

    expect(await screen.findByText(/Vector cannot send email yet/)).toBeInTheDocument()

    await user.type(await screen.findByLabelText('Email address'), 'dev@acme.test')
    await user.click(screen.getByRole('button', { name: 'Create invite link' }))

    await link.resolve('OnboardingInvitationCreate', {
      data: {
        invitationCreate: {
          __typename: 'InvitationCreatePayload',
          invitation: {
            __typename: 'WorkspaceInvitation',
            id: '00000000-0000-4000-8000-00000000c001',
            email: 'dev@acme.test',
            role: 'MEMBER',
            expiresAt: '2026-01-22T12:00:00.000Z',
          },
          token: 'tok_abc123',
          errors: [],
        },
      },
    })

    // The link, in full and selectable -- not only behind a copy button that
    // the clipboard API may refuse. It points at the route this feature also
    // owns, so it is not a dead URL.
    expect(await screen.findByText(/\/invite\/tok_abc123$/)).toBeInTheDocument()
    expect(
      screen.getByRole('button', {
        name: 'Copy invite link for dev@acme.test',
      }),
    ).toBeInTheDocument()
  })
})
