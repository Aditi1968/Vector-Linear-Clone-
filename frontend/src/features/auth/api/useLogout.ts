import { useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { useApolloClient, useMutation } from '@apollo/client/react'

import { publicPaths } from '../../../app/routes/paths'
import { LogoutDocument } from './documents'

export interface UseLogoutResult {
  logout: () => Promise<void>
  isSigningOut: boolean
}

/**
 * End the session and leave nothing of it behind.
 *
 * Three steps, in this order, and the order is the reasoning:
 *
 *   1. `logout` -- the server deletes the session row and clears the cookie.
 *      Only the server can: the cookie is HttpOnly, so there is no
 *      client-side "forget the token" step to take and no token to forget.
 *   2. navigate to the landing page, *before* emptying the cache, so that no
 *      authenticated screen is ever asked to render against a cache that has
 *      just had its data pulled out from under it.
 *   3. `clearStore()` -- drop every cached entity.
 *
 * Step 3 is not tidiness. Vector runs on shared machines; a cache that
 * survives sign-out is the previous account's issues, comments and workspace
 * names still readable by whoever signs in next, and Apollo would serve them
 * from `cache-first` without a request. Emptying it is the only thing that
 * makes signing out mean anything on the client.
 *
 * `clearStore` and not `resetStore`: reset *refetches* every active query,
 * which immediately after signing out means firing protected queries as an
 * anonymous caller and painting their UNAUTHENTICATED errors across the way
 * out. Clear drops the data and asks for nothing.
 *
 * The cleanup is in a `finally`, so a failed or unanswered `logout` still
 * clears the client. The alternative -- keeping the cache because the request
 * did not come back -- optimises for the session possibly still being valid
 * at the cost of leaving another account's data on screen.
 */
export function useLogout(): UseLogoutResult {
  const client = useApolloClient()
  const navigate = useNavigate()
  const [mutate, { loading }] = useMutation(LogoutDocument)

  const logout = useCallback(async (): Promise<void> => {
    try {
      await mutate()
    } catch {
      // Nothing to report and nowhere to report it: the user asked to be
      // signed out, and the client-side half of that happens regardless.
    } finally {
      void navigate(publicPaths.landing(), { replace: true })
      await client.clearStore()
    }
  }, [client, mutate, navigate])

  return { logout, isSigningOut: loading }
}
