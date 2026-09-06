import { ApolloClient } from '@apollo/client'

import { createCache } from '../lib/graphql'
import type { ControlledLink } from './controlledLink'

/**
 * A client with a controllable network and the application's real cache.
 *
 * `createCache()` and not `new InMemoryCache()`: the `issues` field policy is
 * the thing most of these tests are about, and a bare cache would still
 * render rows, still pass a naive "two pages appear" assertion, and prove
 * none of the merge, the dedupe or the reset.
 *
 * Used by ./render.tsx for screen tests, and directly by the tests that drive
 * an `ObservableQuery` -- which is the only way to observe `networkStatus`
 * transitions and `refetch()` behaviour that no screen exposes a control for.
 */
export function createTestClient(link: ControlledLink): ApolloClient {
  return new ApolloClient({
    link,
    cache: createCache(),
  })
}
