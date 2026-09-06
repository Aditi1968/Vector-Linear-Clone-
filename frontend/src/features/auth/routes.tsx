import type { RouteObject } from 'react-router-dom'

import { appPaths } from '../../app/routes/paths'
import { LandingPage } from '../landing'
import { RequireNoAuth } from './guards'
import { LoginPage } from './pages/LoginPage'
import { RegisterPage } from './pages/RegisterPage'

/**
 * Every route a signed-out visitor can reach, as data.
 *
 * Exported as `RouteObject[]` rather than as three components so the
 * application's route table can spread them in one line and never learn the
 * names of the pages behind them. Adding a public route -- a password reset,
 * an invitation acceptance -- happens here and nowhere else.
 *
 * Absolute paths, not relative segments, and that is what makes them
 * composable: they resolve identically at the top level of the table and
 * underneath a parent whose own path is `/`. The workspace-scoped routes are
 * the ones that must stay relative; these three must never be scoped at all.
 *
 * `RequireNoAuth` wraps the two forms and not the landing page. Somebody
 * already signed in has no use for a sign-in form and is redirected to their
 * workspace, but the landing page is a real public page that a signed-in
 * visitor may legitimately want to look at.
 */
export const authRoutes: RouteObject[] = [
  {
    path: appPaths.landing(),
    element: <LandingPage />,
  },
  {
    path: appPaths.login(),
    element: (
      <RequireNoAuth>
        <LoginPage />
      </RequireNoAuth>
    ),
  },
  {
    path: appPaths.register(),
    element: (
      <RequireNoAuth>
        <RegisterPage />
      </RequireNoAuth>
    ),
  },
]
