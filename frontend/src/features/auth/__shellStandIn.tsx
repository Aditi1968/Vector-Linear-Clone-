import type { ReactNode } from 'react'
import type { RouteObject } from 'react-router-dom'

import { RouteError } from '../../app/routes/RouteError'
import { WorkspaceEntry } from '../../app/routes/WorkspaceEntry'

/**
 * TEMPORARY. Delete this file when `features/auth` lands.
 *
 * The app shell composes its route table from `authRoutes`,
 * `onboardingRoutes` and its own workspace routes, and it is being built at
 * the same time as the two features that own the first two. This file exists
 * so that the shell compiles, its tests run, and the interface between them
 * is written down somewhere both halves can read.
 *
 * It implements nothing. `RequireAuth` lets everyone through and `useLogout`
 * does nothing, which is exactly what makes this unfit to ship: an
 * unauthenticated visitor reaches every screen. That is acceptable only for
 * as long as the real module is missing, and the whole file goes when it
 * arrives -- `features/auth/index.ts` is expected to conflict on merge, and
 * the resolution is to take the auth agent's version wholesale.
 *
 * The contract the shell is written against:
 *
 *     authRoutes: RouteObject[]            // '/', '/login', '/register'
 *     RequireAuth: ({ children }) => Element
 *     useLogout: () => () => void | Promise<unknown>
 */

/** Lets everything through. The real one redirects to `/login`. */
export function RequireAuth({ children }: { children: ReactNode }) {
  return <>{children}</>
}

/** Does nothing. The real one calls the `logout` mutation and clears the cache. */
export function useLogout(): () => void {
  return () => {
    // Intentionally empty. See the note at the top of this file.
  }
}

function SignInStandIn() {
  return (
    <main>
      <h1>Sign in</h1>
      <p>Authentication is not wired up in this build.</p>
    </main>
  )
}

/**
 * The public routes.
 *
 * `/` is `WorkspaceEntry`, which resolves the viewer's first workspace and
 * redirects into it -- the behaviour that already exists on `main`. It is
 * exported from `app/routes` so that the real auth feature can keep using it
 * as its signed-in landing rather than reinventing the resolution.
 */
export const authRoutes: RouteObject[] = [
  { index: true, element: <WorkspaceEntry />, errorElement: <RouteError /> },
  { path: 'login', element: <SignInStandIn /> },
  { path: 'register', element: <SignInStandIn /> },
]
