import { render, screen } from '@testing-library/react'
import { RouterProvider, createMemoryRouter } from 'react-router-dom'
import type { RouteObject } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

import { AppProviders } from '../providers/AppProviders'
import { createTestClient } from '../../test/client'
import { ControlledLink } from '../../test/controlledLink'
import { shellSidebarData, workspaceShellData } from '../../test/factories'
import { WORKSPACE_SLUG_PARAM } from './paths'
import { routes } from './routes'

/**
 * The route error boundary.
 *
 * ================================================================
 * Why an untested error boundary is the same shape as a leak
 * ================================================================
 *
 * `errorElement` exists for one purpose: to stop React Router's own fallback
 * rendering the caught error's `stack` into a `<pre>`. That fallback has no
 * environment guard, so it ships in the production bundle -- a user meeting an
 * unexpected error would be handed an internal stack trace and a note
 * addressed to a developer.
 *
 * A protection whose entire job is preventing a leak, with no test, is a
 * guarantee nobody checks. Nothing in the application *uses* `errorElement`
 * in the ordinary sense, so it reads as dead configuration; the plausible
 * future edit is someone deleting it because nothing seems to reference it.
 * These tests are what object.
 *
 * ================================================================
 * How the throw is driven
 * ================================================================
 *
 * The real route table is reused and one throwing child is appended to it,
 * rather than a fresh table being written here. That is deliberate: the
 * spread carries the root route's `errorElement` along with everything else,
 * so these tests run against the *actual* wiring. Delete `errorElement` from
 * ./routes.tsx and this file fails, which is the whole point. A hand-built
 * table with its own `errorElement` would keep passing and prove nothing.
 */

const LEAKY_MESSAGE =
  'Connection to db-primary-7 failed: password authentication failed for user "vector_app"'

/**
 * Constructed once so its `stack` is available to assert against. A stack
 * generated inside the component would be a value the test cannot see, and
 * "the stack is absent" would then be an assertion about nothing.
 */
const THROWN = new Error(LEAKY_MESSAGE)

const STACK_FRAMES = (THROWN.stack ?? '')
  .split('\n')
  .map((line) => line.trim())
  .filter((line) => line.startsWith('at '))
  .slice(0, 3)

function Exploding(): never {
  throw THROWN
}

/** The real table, plus one route that fails inside the workspace shell. */
function routesWith(extra: RouteObject): RouteObject[] {
  // Selected by path rather than by position. The table is composed from the
  // auth, onboarding and workspace route sets, so an index into it is a claim
  // about which feature is listed first -- and the shell is the one under
  // `/:workspaceSlug`, whichever order they end up in.
  const shell = routes.find(
    (route) => route.index !== true && route.path === `/:${WORKSPACE_SLUG_PARAM}`,
  )

  if (shell === undefined || shell.index === true) {
    throw new Error('The route table has no workspace shell route')
  }

  return [
    {
      // Carries the real `errorElement`. See the note at the top of the file.
      ...shell,
      children: [...(shell.children ?? []), extra],
    },
  ]
}

function renderAt(table: RouteObject[], path: string): void {
  const link = new ControlledLink()

  // The shell will not render a child until it knows the viewer belongs to
  // the workspace in the URL, so the throwing route below would never mount
  // without these. See `ControlledLink.answerAlways`.
  link.answerAlways('WorkspaceShell', { data: workspaceShellData() })
  link.answerAlways('ShellSidebar', { data: shellSidebarData() })

  const client = createTestClient(link)
  const router = createMemoryRouter(table, { initialEntries: [path] })

  render(
    <AppProviders client={client}>
      <RouterProvider router={router} />
    </AppProviders>,
  )
}

describe('RouteError', () => {
  it('reports a thrown error without revealing anything about it', () => {
    // React logs every error an boundary catches, and `RouteError` logs the
    // detail itself. Silenced so the run stays readable, and asserted below.
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined)

    renderAt(routesWith({ path: 'explode', element: <Exploding /> }), '/acme/explode')

    // Something controlled rendered: not a blank page, and not a crash that
    // took the render down with it.
    expect(
      screen.getByRole('heading', { level: 1, name: 'Something went wrong' }),
    ).toBeInTheDocument()

    // And a way out, which is the difference between an error page and a
    // dead end.
    expect(screen.getByRole('link', { name: 'Back to Vector' })).toHaveAttribute(
      'href',
      '/',
    )

    const page = document.body.textContent ?? ''

    // Nothing of the error itself. The message is written to look like a real
    // leak -- a hostname and a database role -- because that is what a caught
    // exception actually carries.
    expect(page).not.toContain(LEAKY_MESSAGE)
    expect(page).not.toContain('db-primary-7')
    expect(page).not.toContain('vector_app')

    // And no stack. `<pre>` is how React Router's fallback renders one, so
    // its absence is checked directly as well as frame by frame.
    expect(document.querySelector('pre')).toBeNull()
    expect(STACK_FRAMES.length).toBeGreaterThan(0)
    for (const frame of STACK_FRAMES) {
      expect(page).not.toContain(frame)
    }

    // Not React Router's developer-facing fallback. These two strings are
    // what appears if `errorElement` is removed from the route table.
    expect(page).not.toContain('Unexpected Application Error')
    expect(page).not.toContain('Hey developer')

    /*
      The detail is not lost, only withheld from the page: a developer needs
      it and the console is where they are meant to find it. The call is
      guarded by `import.meta.env.DEV` so it never ships, and this runner is
      not a production build, so it fires here.
    */
    expect(consoleError).toHaveBeenCalledWith('Unhandled route error', THROWN)
  })

  it('shows the status of a thrown route response', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined)

    renderAt(
      routesWith({
        path: 'missing',
        loader: () => {
          /*
            `only-throw-error` is disabled on the next line, and only there.

            Throwing a `Response` is React Router's own idiom for a route
            error carrying a status, and it is exactly what
            `isRouteErrorResponse` in ./RouteError.tsx exists to recognise.
            There is no Error-shaped way to produce one, so satisfying the
            rule here would mean testing something other than the branch
            under test.
          */
          // eslint-disable-next-line @typescript-eslint/only-throw-error
          throw new Response(null, { status: 404, statusText: 'Not Found' })
        },
        element: <div />,
      }),
      '/acme/missing',
    )

    // A router-thrown response carries a status worth showing, unlike an
    // unexpected exception, which gets one flat message.
    expect(
      await screen.findByRole('heading', { level: 1, name: 'Error 404' }),
    ).toBeInTheDocument()

    expect(document.querySelector('pre')).toBeNull()
    expect(document.body.textContent).not.toContain('Unexpected Application Error')
  })

  it('renders standalone rather than inside the shell, deliberately', () => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined)

    renderAt(routesWith({ path: 'explode', element: <Exploding /> }), '/acme/explode')

    /*
      `RouteError` owns its own `<main>`, and that is *not* the duplicate
      landmark that `NotFound` next door is careful to avoid.

      The two are different situations. `NotFound` renders inside `AppLayout`,
      which already provides the landmark, so adding a second would be a
      genuine fault. `RouteError` replaces the root route's element -- the
      shell is gone -- so it must provide the landmark itself, and it is
      deliberately not wrapped in `AppLayout`: if the shell is what threw,
      rendering the shell again to report the failure throws a second time and
      loses the message entirely.

      So: exactly one `main`, and no shell around it.
    */
    expect(screen.getAllByRole('main')).toHaveLength(1)
    expect(screen.queryByRole('navigation', { name: 'Main' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'New issue' })).toBeNull()
  })
})
