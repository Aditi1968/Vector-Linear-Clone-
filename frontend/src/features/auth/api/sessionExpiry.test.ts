import { ApolloClient, ApolloLink, InMemoryCache } from '@apollo/client'
import { Observable } from 'rxjs'
import { describe, expect, it } from 'vitest'

import { MeDocument } from './documents'
import { sessionExpiryLink } from './sessionExpiry'

/**
 * The session dying underneath a running application.
 *
 * Worth a test of its own because it is unreachable from a screen: no button
 * revokes your session, and the failure -- every page showing an error panel
 * while the cache still insists you are signed in -- only appears after a
 * session has actually expired in production.
 *
 * The client here is built by hand rather than through `createTestClient`,
 * because what is under test is the *link chain*, and the shared test client
 * deliberately has only a transport in it.
 */

const VIEWER = {
  __typename: 'User' as const,
  id: '00000000-0000-4000-8000-0000000000a1',
  email: 'ada@example.com',
  name: 'Ada',
}

/** A link that answers every operation with the same canned result. */
function respondWith(result: Record<string, unknown>): ApolloLink {
  return new ApolloLink(
    () =>
      new Observable<never>((subscriber) => {
        subscriber.next(result as never)
        subscriber.complete()
      }),
  )
}

function clientAnswering(result: Record<string, unknown>): ApolloClient {
  const client = new ApolloClient({
    link: ApolloLink.from([sessionExpiryLink, respondWith(result)]),
    cache: new InMemoryCache(),
  })

  client.cache.writeQuery({ query: MeDocument, data: { me: VIEWER } })

  return client
}

function cachedViewer(client: ApolloClient): unknown {
  return client.cache.readQuery({ query: MeDocument })
}

describe('sessionExpiryLink', () => {
  it('forgets the viewer when a query comes back UNAUTHENTICATED', async () => {
    const client = clientAnswering({
      errors: [
        {
          message: 'Authentication required',
          extensions: { code: 'UNAUTHENTICATED' },
        },
      ],
    })

    await client
      .query({ query: MeDocument, fetchPolicy: 'network-only' })
      .catch(() => undefined)

    // `me: null` rather than an empty cache: the guards already redirect a
    // null viewer to `/login` carrying the current URL, so this reuses that
    // path instead of adding a second way to leave a page. Clearing the whole
    // store mid-flight is what `useLogout` does, deliberately, and not here.
    expect(cachedViewer(client)).toEqual({ me: null })
  })

  it('leaves the viewer alone for any other error', async () => {
    const client = clientAnswering({
      errors: [
        { message: 'Issue not found', extensions: { code: 'NOT_FOUND' } },
      ],
    })

    await client
      .query({ query: MeDocument, fetchPolicy: 'network-only' })
      .catch(() => undefined)

    // A missing issue is not a missing session. Signing someone out because
    // one field 404'd would be the more damaging bug of the two.
    expect(cachedViewer(client)).toEqual({ me: VIEWER })
  })
})
