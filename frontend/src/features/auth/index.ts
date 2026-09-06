/**
 * The session layer.
 *
 * What the rest of the application depends on: who is signed in
 * (`useViewer`), the two guards that decide whether a URL may be rendered,
 * the sign-out action, and this feature's public routes as data.
 *
 * The one rule for consumers: there is no session object to pass around and
 * nothing to store. The session is an HttpOnly cookie the browser holds and
 * JavaScript cannot read; `useViewer()` asks the server who that is and the
 * Apollo cache remembers the answer. Any code that writes a token, a password
 * or a user to `localStorage` is a bug, not an optimisation.
 */

export { useLogout, useViewer } from './api'
export type { UseLogoutResult, UseViewerResult, Viewer } from './api'

export { RequireAuth, RequireNoAuth } from './guards'
export type { GuardProps } from './guards'

export { authRoutes } from './routes'
