import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { UserEvent } from '@testing-library/user-event'
import { RouterProvider, createMemoryRouter } from 'react-router-dom'
import type { ApolloClient } from '@apollo/client'

import { describe, expect, it } from 'vitest'

import { AppProviders } from '../../app/providers/AppProviders'
import { Button } from '../../components'
import { ControlledLink } from '../../test/controlledLink'
import { createTestClient } from '../../test/client'
import type {
  LoginMutation,
  MeQuery,
  MyWorkspacesQuery,
  RegisterMutation,
} from '../../generated/operations'
import { RequireAuth, authRoutes } from '.'
import { useLogout } from './api'

/**
 * The session layer.
 *
 * Four things are tested here and they are the four that break silently.
 *
 *   1. A guard that lets the wrong person through, or bounces the right one,
 *      is either a hole or a lock-out -- and both look like a working app in
 *      development, where the developer is always signed in.
 *   2. Where a successful sign-in *lands* depends on server state
 *      (`myWorkspaces`), so it is exactly the kind of thing that gets
 *      hard-coded to whatever the author's own account happened to be.
 *   3. Error mapping. A `field` the form does not attach to an input
 *      disappears, and the visible symptom is a form that does nothing.
 *   4. Signing out has to empty the cache. Nothing on screen shows that it
 *      did, and the cost of it not happening is one account reading another
 *      account's data on a shared machine.
 *
 * Everything above the network is real: the real guards, the real forms, the
 * real routes, the real cache. Only the transport is controlled.
 */

const VIEWER: NonNullable<MeQuery['me']> = {
  __typename: 'User',
  id: '00000000-0000-4000-8000-0000000000a1',
  email: 'ada@example.com',
  name: 'Ada',
}

function meData(me: MeQuery['me']): MeQuery {
  return { me }
}

function workspacesData(slugs: readonly string[]): MyWorkspacesQuery {
  return {
    myWorkspaces: slugs.map((slug, index) => ({
      __typename: 'WorkspaceMembership' as const,
      role: 'MEMBER' as const,
      workspace: {
        __typename: 'Workspace' as const,
        id: `00000000-0000-4000-8000-00000000b0${String(index)}0`,
        slug,
        name: slug,
      },
    })),
  }
}

function loginRejected(
  field: string,
  code: string,
  message: string,
): LoginMutation {
  return {
    login: {
      __typename: 'LoginPayload',
      user: null,
      errors: [{ __typename: 'ValidationErrorType', field, code, message }],
    },
  }
}

function registerRejected(
  field: string,
  code: string,
  message: string,
): RegisterMutation {
  return {
    register: {
      __typename: 'RegisterPayload',
      user: null,
      errors: [{ __typename: 'ValidationErrorType', field, code, message }],
    },
  }
}

/** A page only a signed-in visitor may see. */
function ProtectedPage() {
  return <h1>Team dashboard</h1>
}

/** Somewhere to press "sign out" from. */
function SignOutPage() {
  const { logout } = useLogout()

  return (
    <Button
      onClick={() => {
        void logout()
      }}
    >
      Sign out
    </Button>
  )
}

interface RenderResult {
  link: ControlledLink
  client: ApolloClient
  user: UserEvent
  currentPath: () => string
}

/**
 * Mount this feature's real routes plus the destinations they redirect to.
 *
 * The stand-ins for `/onboarding` and `/:workspaceSlug/issues` are other
 * agents' screens, so they are represented by their headings only. What is
 * under test is which of them the guard picks, not what they render.
 */
function renderAuth(initialPath: string): RenderResult {
  const link = new ControlledLink()
  const client = createTestClient(link)

  const router = createMemoryRouter(
    [
      ...authRoutes,
      {
        path: '/protected/:thing',
        element: (
          <RequireAuth>
            <ProtectedPage />
          </RequireAuth>
        ),
      },
      {
        path: '/signed-in',
        element: (
          <RequireAuth>
            <SignOutPage />
          </RequireAuth>
        ),
      },
      { path: '/onboarding', element: <h1>Create a workspace</h1> },
      { path: '/:workspaceSlug/issues', element: <h1>Workspace issues</h1> },
    ],
    { initialEntries: [initialPath] },
  )

  render(
    <AppProviders client={client}>
      <RouterProvider router={router} />
    </AppProviders>,
  )

  return {
    link,
    client,
    user: userEvent.setup(),
    currentPath: () => router.state.location.pathname,
  }
}

describe('RequireAuth', () => {
  it('sends a signed-out visitor to sign in and brings them back afterwards', async () => {
    const { link, user, currentPath } = renderAuth('/protected/roadmap')

    await link.resolve('Me', { data: meData(null) })

    expect(currentPath()).toBe('/login')

    await user.type(screen.getByLabelText('Email'), 'ada@example.com')
    await user.type(screen.getByLabelText('Password'), 'correct-horse')
    await user.click(screen.getByRole('button', { name: 'Sign in' }))

    await link.resolve('Login', {
      data: {
        login: { __typename: 'LoginPayload', user: VIEWER, errors: [] },
      } satisfies LoginMutation,
    })

    // The URL they originally asked for, not a generic landing spot -- and
    // reached without a second `Me` round trip, because the mutation wrote
    // the viewer into the cache.
    expect(currentPath()).toBe('/protected/roadmap')
    expect(screen.getByRole('heading', { name: 'Team dashboard' })).toBeInTheDocument()
    expect(link.countOf('Me')).toBe(1)
  })

  it('renders the page for a signed-in viewer', async () => {
    const { link, currentPath } = renderAuth('/protected/roadmap')

    await link.resolve('Me', { data: meData(VIEWER) })

    expect(currentPath()).toBe('/protected/roadmap')
    expect(screen.getByRole('heading', { name: 'Team dashboard' })).toBeInTheDocument()
  })

  it('offers a retry instead of signing someone out when the check fails', async () => {
    const { link, currentPath } = renderAuth('/protected/roadmap')

    await link.fail('Me', new Error('network down'))

    // Still on the protected URL. A failed question is not a "no": treating
    // it as one would throw people out of live sessions on every blip.
    expect(currentPath()).toBe('/protected/roadmap')
    expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument()
  })
})

describe('RequireNoAuth', () => {
  it('sends an account with no workspace to onboarding', async () => {
    const { link, currentPath } = renderAuth('/login')

    await link.resolve('Me', { data: meData(VIEWER) })
    await link.resolve('MyWorkspaces', { data: workspacesData([]) })

    expect(currentPath()).toBe('/onboarding')
  })

  it('sends an account with a workspace into it', async () => {
    const { link, currentPath } = renderAuth('/register')

    await link.resolve('Me', { data: meData(VIEWER) })
    await link.resolve('MyWorkspaces', { data: workspacesData(['acme']) })

    expect(currentPath()).toBe('/acme/issues')
  })

  it('shows the sign-in form to a signed-out visitor', async () => {
    const { link, currentPath } = renderAuth('/login')

    await link.resolve('Me', { data: meData(null) })

    expect(currentPath()).toBe('/login')
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeInTheDocument()
    // `myWorkspaces` is protected; asking it without a session would answer
    // UNAUTHENTICATED and put an error in front of an ordinary visitor.
    expect(link.countOf('MyWorkspaces')).toBe(0)
  })
})

describe('server error mapping', () => {
  it('shows a refused log-in above the form, because no input is at fault', async () => {
    const { link, user, currentPath } = renderAuth('/login')

    await link.resolve('Me', { data: meData(null) })

    await user.type(screen.getByLabelText('Email'), 'ada@example.com')
    await user.type(screen.getByLabelText('Password'), 'wrong')
    await user.click(screen.getByRole('button', { name: 'Sign in' }))

    await link.resolve('Login', {
      data: loginRejected(
        'credentials',
        'INVALID_CREDENTIALS',
        'Email or password is incorrect',
      ),
    })

    // The backend reports this under `credentials` precisely so the client
    // cannot say which half was wrong. Attaching it to the email input would
    // undo that and turn the form into an account-enumeration oracle.
    expect(screen.getByRole('alert')).toHaveTextContent(
      'Email or password is incorrect',
    )
    expect(screen.getByLabelText('Email')).not.toHaveAttribute('aria-invalid')
    expect(currentPath()).toBe('/login')
  })

  it('attaches a field error to its input, describes it, and focuses it', async () => {
    const { link, user } = renderAuth('/register')

    await link.resolve('Me', { data: meData(null) })

    await user.type(screen.getByLabelText('Email'), 'taken@example.com')
    await user.type(screen.getByLabelText('Password'), 'correct-horse')
    await user.click(screen.getByRole('button', { name: 'Create account' }))

    await link.resolve('Register', {
      data: registerRejected(
        'email',
        'EMAIL_TAKEN',
        'An account with this email already exists',
      ),
    })

    const email = screen.getByLabelText('Email')

    expect(email).toHaveAttribute('aria-invalid', 'true')
    expect(email).toHaveAccessibleDescription(
      /An account with this email already exists/,
    )
    // Focus, because a message rendered somewhere on the page is a form that
    // silently did nothing as far as a screen reader is concerned.
    expect(email).toHaveFocus()
  })

  it('reports a transport failure without blaming a field', async () => {
    const { link, user } = renderAuth('/login')

    await link.resolve('Me', { data: meData(null) })

    await user.type(screen.getByLabelText('Email'), 'ada@example.com')
    await user.type(screen.getByLabelText('Password'), 'correct-horse')
    await user.click(screen.getByRole('button', { name: 'Sign in' }))

    await link.fail('Login', new Error('connection reset'))

    const alert = screen.getByRole('alert')

    expect(alert).toHaveTextContent('Something went wrong. Please try again.')
    // Never the raw failure: it is neither actionable nor vetted for what it
    // might disclose about the server.
    expect(alert).not.toHaveTextContent('connection reset')
    expect(screen.getByLabelText('Email')).not.toHaveAttribute('aria-invalid')
  })
})

describe('useLogout', () => {
  it('empties the cache and returns to the landing page', async () => {
    const { link, client, user, currentPath } = renderAuth('/signed-in')

    await link.resolve('Me', { data: meData(VIEWER) })

    expect(client.cache.extract()).toHaveProperty(`User:${VIEWER.id}`)

    await user.click(screen.getByRole('button', { name: 'Sign out' }))
    await link.resolve('Logout', {
      data: { logout: { __typename: 'LogoutPayload', signedOut: true, errors: [] } },
    })

    expect(currentPath()).toBe('/')
    // The assertion that matters: not that a mutation was sent, but that the
    // previous account's entities are gone. Apollo would otherwise serve them
    // to the next person to sign in on this machine, from `cache-first`, with
    // no request.
    expect(client.cache.extract()).not.toHaveProperty(`User:${VIEWER.id}`)
  })

  it('still empties the cache when the sign-out request fails', async () => {
    const { link, client, user, currentPath } = renderAuth('/signed-in')

    await link.resolve('Me', { data: meData(VIEWER) })

    await user.click(screen.getByRole('button', { name: 'Sign out' }))
    await link.fail('Logout', new Error('connection reset'))

    // Keeping the cache because the request did not come back optimises for
    // the session maybe still being valid, at the cost of leaving another
    // account's data on screen. The wrong trade.
    expect(currentPath()).toBe('/')
    expect(client.cache.extract()).not.toHaveProperty(`User:${VIEWER.id}`)
  })
})
