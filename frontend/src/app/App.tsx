import { RouterProvider, createBrowserRouter } from 'react-router-dom'

import { AppProviders } from './providers/AppProviders'
import { routes } from './routes/routes'

/**
 * The router instance.
 *
 * Built once at module scope, not inside `App`. `createBrowserRouter` owns
 * history and the current match; rebuilding it on a render resets both, so a
 * router created in the component body would drop the user back to the entry
 * URL on every re-render of the tree above it.
 */
const router = createBrowserRouter(routes)

/**
 * The application root.
 *
 * Providers wrap the router rather than the other way round, so that route
 * elements -- and anything a route lazily loads -- can use Apollo hooks.
 */
export function App() {
  return (
    <AppProviders>
      <RouterProvider router={router} />
    </AppProviders>
  )
}
