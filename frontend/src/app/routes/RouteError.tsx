import { isRouteErrorResponse, useRouteError } from 'react-router-dom'

/**
 * What a user sees when a route throws.
 *
 * Without an `errorElement`, React Router falls back to its own boundary,
 * and that fallback is not a production surface: it renders the caught
 * error's `stack` into a `<pre>` with no environment guard, alongside a
 * message addressed to the developer. Both ship in the production bundle,
 * so a user meeting an unexpected error is handed an internal stack trace
 * and a note apologising to somebody else.
 *
 * The backend already refuses to do this -- its GraphQL layer masks every
 * unexpected exception to a generic message precisely so that internals do
 * not reach a client. This is the same rule at the other end of the wire.
 *
 * Deliberately standalone rather than rendered inside `AppLayout`: if the
 * shell itself is what threw, rendering the shell again to report the
 * failure would throw a second time and lose the message entirely.
 *
 * A route response (a 404 thrown by the router) carries a status worth
 * showing; anything else is an unexpected error and gets one flat message.
 * The error is still sent to the console, because a developer needs the
 * detail that the page deliberately withholds.
 */
export function RouteError() {
  const error = useRouteError()

  if (import.meta.env.DEV) {
    // The page withholds this detail on purpose; the console is where a
    // developer is meant to find it. Guarded so it never ships.
    console.error('Unhandled route error', error)
  }

  const status = isRouteErrorResponse(error) ? error.status : null

  return (
    <main className="routeError">
      <h1>{status === null ? 'Something went wrong' : `Error ${status}`}</h1>

      <p>
        Vector hit an unexpected problem rendering this page. Reloading may
        clear it.
      </p>

      <p>
        <a href="/">Back to Vector</a>
      </p>
    </main>
  )
}
