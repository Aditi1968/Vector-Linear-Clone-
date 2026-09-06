import { Navigate, Outlet, useLocation } from 'react-router-dom'
import type { ReactNode } from 'react'

import { ErrorState, Spinner, VisuallyHidden } from '../../components'
import { useAppPaths } from '../../app/routes/useAppPaths'
import { useSignedInDestination, useViewer } from './api'
import styles from './auth.module.css'

/**
 * Where the visitor was heading when the guard turned them away, carried in
 * router state so sign-in can put them back.
 *
 * Router state rather than a `?next=` parameter, and that is a security
 * choice rather than a stylistic one: a return path in the URL is attacker-
 * supplied, and every application that has one eventually grows an open
 * redirect. State is only ever written by the code below.
 */
interface ReturnState {
  from: string
}

/**
 * The stored return path, if there is a credible one.
 *
 * Validated even though only this file writes it. `history.state` survives a
 * reload and is editable from the console, so the value reaching `<Navigate>`
 * is not trustworthy by provenance. Same-origin absolute paths only: `//host`
 * and `/\host` are protocol-relative URLs that browsers resolve off-site.
 */
function returnPathFrom(state: unknown): string | null {
  if (typeof state !== 'object' || state === null || !('from' in state)) {
    return null
  }

  const { from } = state as Partial<ReturnState>

  if (typeof from !== 'string' || !from.startsWith('/')) {
    return null
  }

  if (from.startsWith('//') || from.startsWith('/\\')) {
    return null
  }

  return from
}

/**
 * The wait while the server is asked who this is.
 *
 * Announced rather than silent: the guards sit between a URL and its page, so
 * a screen reader would otherwise get a blank document and no explanation for
 * however long the round trip takes.
 */
function SessionPending({ label }: { label: string }) {
  return (
    <div className={styles.pending} role="status">
      <Spinner />
      <VisuallyHidden>{label}</VisuallyHidden>
    </div>
  )
}

export interface GuardProps {
  /** Guarded content. Omitted when the guard wraps a route as an element. */
  children?: ReactNode
}

/**
 * Admit only a signed-in viewer.
 *
 * Renders one of four things and never guesses between them: the wait, a
 * failure with a retry, a redirect to sign-in, or the page.
 *
 * The distinction that matters is the third against the second. `me` returning
 * null means nobody is signed in and the answer is the sign-in page. `me`
 * *failing* means the question was not answered -- and treating that as
 * "signed out" would throw people out of a perfectly valid session every time
 * a request timed out, discarding whatever they were in the middle of.
 */
export function RequireAuth({ children }: GuardProps) {
  const { viewer, isLoading, errorMessage, refresh } = useViewer()
  const location = useLocation()
  const paths = useAppPaths()

  if (isLoading) {
    return <SessionPending label="Checking your session" />
  }

  if (errorMessage !== null) {
    return (
      <ErrorState
        title="Could not check your session"
        description={errorMessage}
        onRetry={refresh}
      />
    )
  }

  if (viewer === null) {
    const from = `${location.pathname}${location.search}${location.hash}`

    // `replace`, so the back button does not walk into the page they were
    // just refused and bounce them here again.
    return <Navigate to={paths.login()} replace state={{ from } satisfies ReturnState} />
  }

  return <>{children ?? <Outlet />}</>
}

/**
 * Keep a signed-in viewer off the sign-in and registration pages.
 *
 * Also the one place that decides where signing in *lands*, which is why the
 * forms below do not navigate at all: a successful log-in writes the viewer
 * into the cache, this guard re-renders with somebody signed in, and the
 * redirect happens here. One rule, one place -- rather than the same routing
 * decision written once in the login form, once in the registration form and
 * once here, drifting apart at the first change.
 *
 * The destination is the URL the visitor was originally refused, if a guard
 * saved one, and otherwise whatever `myWorkspaces` says: no membership means
 * onboarding, any membership means a workspace.
 *
 * A failed `me` renders the public page. It is the safe default -- the worst
 * case is showing a sign-in form to somebody already signed in, and the
 * server refuses to sign anyone in twice.
 */
export function RequireNoAuth({ children }: GuardProps) {
  const { viewer, isLoading } = useViewer()
  const location = useLocation()
  const signedIn = viewer !== null
  const returnPath = returnPathFrom(location.state)

  // A saved return path is already an answer, so `myWorkspaces` is not asked
  // for another one. That skips a round trip on the commonest path through
  // this screen -- follow a link, get refused, sign in -- and is also what
  // makes that redirect immediate rather than one request late.
  const { destination, isResolving, errorMessage, retry } = useSignedInDestination(
    signedIn && returnPath === null,
  )

  if (isLoading || (signedIn && returnPath === null && isResolving)) {
    return <SessionPending label="Checking your session" />
  }

  if (signedIn && returnPath !== null) {
    return <Navigate to={returnPath} replace />
  }

  if (signedIn && errorMessage !== null) {
    return (
      <ErrorState
        title="Could not open your workspace"
        description={errorMessage}
        onRetry={retry}
      />
    )
  }

  if (signedIn && destination !== null) {
    return <Navigate to={destination} replace />
  }

  return <>{children ?? <Outlet />}</>
}
