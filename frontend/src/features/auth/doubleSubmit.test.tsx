import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { RouterProvider, createMemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'

import { AppProviders } from '../../app/providers/AppProviders'
import { ControlledLink } from '../../test/controlledLink'
import { createTestClient } from '../../test/client'
import { authRoutes } from '.'

/**
 * One submit is one request, whichever way the person submits.
 *
 * These two forms are the ones where a second in-flight request is worst.
 * `register` creates a row: two of them is either a duplicate account or a
 * confusing `EMAIL_TAKEN` refusal of the visitor's own first attempt, arriving
 * a moment after they were signed in. `login` consumes the auth rate-limit
 * budget -- `AuthService.log_in` counts every attempt before it does anything
 * else -- so an impatient double click spends two of a small allowance and
 * moves the visitor closer to a lockout for pressing a button twice.
 *
 * The guard is `Button`'s `loading` prop, and it is `aria-disabled` rather
 * than `disabled` on purpose (see the note in components/Button): `disabled`
 * drops the element out of the tab order, and the browser then moves focus to
 * `<body>` -- so a keyboard user who pressed Enter on "Sign in" loses their
 * place at the exact moment the app starts working. The click handler is what
 * makes it inert.
 *
 * Which is why Enter is tested separately from the click. Implicit form
 * submission from a text input is not the same event path as a pointer press,
 * and a guard that only covers `onClick` is a guard with a hole in it that no
 * mouse-driven test would ever find.
 */

function renderAuthForm(initialPath: string) {
  const link = new ControlledLink()
  const client = createTestClient(link)

  const router = createMemoryRouter(
    [
      ...authRoutes,
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

  return { link, user: userEvent.setup() }
}

describe('signing in twice by accident', () => {
  it('sends one Login for a double click', async () => {
    const { link, user } = renderAuthForm('/login')

    await link.resolve('Me', { data: { me: null } })

    await user.type(screen.getByLabelText('Email'), 'ada@example.com')
    await user.type(screen.getByLabelText('Password'), 'correct-horse')
    await user.dblClick(screen.getByRole('button', { name: 'Sign in' }))
    await link.idle()

    expect(link.countOf('Login')).toBe(1)
  })

  it('sends one Login when Enter is pressed twice in the password box', async () => {
    const { link, user } = renderAuthForm('/login')

    await link.resolve('Me', { data: { me: null } })

    await user.type(screen.getByLabelText('Email'), 'ada@example.com')
    await user.type(screen.getByLabelText('Password'), 'correct-horse{Enter}{Enter}')
    await link.idle()

    expect(link.countOf('Login')).toBe(1)
  })
})

describe('registering twice by accident', () => {
  it('sends one Register for a double click', async () => {
    const { link, user } = renderAuthForm('/register')

    await link.resolve('Me', { data: { me: null } })

    await user.type(screen.getByLabelText('Email'), 'ada@example.com')
    await user.type(screen.getByLabelText('Password'), 'correct-horse')
    await user.dblClick(screen.getByRole('button', { name: 'Create account' }))
    await link.idle()

    expect(link.countOf('Register')).toBe(1)
  })

  it('offers nothing to press twice between the mutation and the redirect', async () => {
    /*
      The form does not navigate on success -- `RequireNoAuth` does, off the
      viewer this mutation writes into the cache. The gap between the two is
      the window this file is about: if the form were still sitting there
      looking pressable, a second account is one impatient click away.

      This test originally asserted the button was still on screen with
      `aria-disabled="true"`. It is not, and that is the better answer: the
      shell swaps the whole form for its pending state, so there is no submit
      control to press at all. The assertion was a guess about the mechanism
      that would deliver the property; what matters is the property.

      So it asserts both halves of that instead -- nothing pressable, and
      exactly one Register on the wire -- and it does the second by trying.
    */
    const { link, user } = renderAuthForm('/register')

    await link.resolve('Me', { data: { me: null } })

    await user.type(screen.getByLabelText('Email'), 'ada@example.com')
    await user.type(screen.getByLabelText('Password'), 'correct-horse')
    await user.click(screen.getByRole('button', { name: 'Create account' }))

    await link.resolve('Register', {
      data: {
        register: {
          __typename: 'RegisterPayload',
          user: {
            __typename: 'User',
            id: '00000000-0000-4000-8000-0000000000a1',
            email: 'ada@example.com',
            name: null,
          },
          errors: [],
        },
      },
    })

    // `myWorkspaces` is what the guard is waiting on, so the redirect has not
    // completed. What is on screen in that window is the pending state, and
    // there is no submit control in it -- queried rather than assumed, so
    // this fails if the form ever comes back looking idle.
    expect(screen.queryByRole('button', { name: 'Create account' })).toBeNull()
    expect(screen.getByRole('status')).toBeInTheDocument()

    // And the property the absence exists to deliver. Pressing Enter is the
    // one route back into a form with no visible button, so try that too --
    // a submit handler still bound to a hidden form would fire here.
    await user.keyboard('{Enter}')
    await link.idle()

    expect(link.countOf('Register')).toBe(1)
  })
})
