import { ApolloClient, ApolloLink, HttpLink } from '@apollo/client'

import { graphqlUrl } from '../config'
import { sessionExpiryLink } from '../../features/auth/api/sessionExpiry'
import { createCache } from './cache'

/**
 * The application's single Apollo boundary.
 *
 * No component may construct an `ApolloClient`. A client owns a cache, and a
 * client built inside a component is rebuilt on every render that does not
 * memoise it -- which throws the cache away, refetches everything, and reads
 * as a network problem rather than a lifecycle one.
 *
 * Note for anyone porting Apollo v3 code: this is v4, where `link` and
 * `cache` are both required options and the `uri` shorthand no longer
 * exists. React bindings also moved -- `ApolloProvider` and the hooks come
 * from `@apollo/client/react`, while `ApolloClient`, `HttpLink`,
 * `InMemoryCache` and `gql` come from `@apollo/client`.
 */
export function createApolloClient(): ApolloClient {
  return new ApolloClient({
    /*
     * Two links, and the order is the contract: a response travels back up
     * the chain, so `sessionExpiryLink` sits above the transport in order to
     * see what the transport returned.
     *
     * It is here rather than inside React because a dead session has to be
     * noticed on *any* operation from any feature, including ones written
     * after this file. See features/auth/api/sessionExpiry.
     */
    link: ApolloLink.from([
      sessionExpiryLink,
      new HttpLink({
        uri: graphqlUrl,

        // Same-origin in development because Vite proxies `/graphql` (see
        // vite.config.ts), and same-origin in production because the endpoint
        // is a path. Stating it rather than inheriting fetch's default is what
        // carries the session: the backend's cookie is HttpOnly, so this
        // header is the only way the browser proves who it is, and it rides
        // along without any CORS credential negotiation precisely because
        // nothing here is cross-origin.
        credentials: 'same-origin',

        // Do NOT set `useGETForQueries`. The backend builds its router with
        // `allow_queries_via_get=False` (`app/graphql/router.py`), so a GET
        // query is refused at the transport, not answered from a cache.
      }),
    ]),

    cache: createCache(),

    // Apollo DevTools in development only. In a production build this keeps
    // the client from advertising itself, and the schema with it.
    devtools: { enabled: import.meta.env.DEV },
  })
}

/**
 * The client the running application uses.
 *
 * Constructed once at module scope, which is the point: importing this
 * module is how a caller gets *the* client rather than one of several.
 * Tests that need isolation call `createApolloClient()` instead.
 */
export const apolloClient = createApolloClient()
