import { CombinedGraphQLErrors } from '@apollo/client/errors'
import { ErrorLink } from '@apollo/client/link/error'

import { MeDocument } from './documents'

/**
 * The code `app/graphql/viewer.py` puts in `extensions` when a request has no
 * usable session. Published deliberately (`PUBLIC_ERROR_CODES` in
 * `app/graphql/schema.py`) precisely so a client can act on it.
 */
const UNAUTHENTICATED = 'UNAUTHENTICATED'

/**
 * Notice when the session has died underneath a running application.
 *
 * Sessions expire, are revoked, and are signed out from another tab. When
 * that happens the next protected query comes back UNAUTHENTICATED while the
 * cache still holds a viewer, so every screen keeps believing it is signed in
 * and renders an error panel instead of the sign-in page.
 *
 * The fix is one write: set `me` to null. The guards already redirect a null
 * viewer to `/login` with the current URL in tow, so this reuses that path
 * rather than adding a second, parallel way to leave a page. There is no
 * event bus, no callback registry, and nothing for a component to subscribe
 * to -- the cache is the notification.
 *
 * It has to be a link rather than something inside React because this must
 * catch *any* query, from any feature, including ones written after this
 * file. `operation.client` is how Apollo Client v4 hands a link the client
 * executing it, which is what makes reaching the cache from here possible at
 * all.
 *
 * Deliberately not clearing the whole store: this runs while an operation is
 * in flight, and emptying the cache under Apollo's feet mid-flight is how a
 * "signed out" turns into a crash. Signing out on purpose (`useLogout`) is
 * the place that clears.
 */
export const sessionExpiryLink = new ErrorLink(({ error, operation }) => {
  if (!CombinedGraphQLErrors.is(error)) {
    return
  }

  const expired = error.errors.some(
    (entry) => entry.extensions?.code === UNAUTHENTICATED,
  )

  if (!expired) {
    return
  }

  operation.client.cache.writeQuery({ query: MeDocument, data: { me: null } })
})
