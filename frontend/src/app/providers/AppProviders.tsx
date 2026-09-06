import { ApolloProvider } from '@apollo/client/react'
import type { ApolloClient } from '@apollo/client'
import type { ReactNode } from 'react'

import { apolloClient } from '../../lib/graphql'

/**
 * Every cross-cutting provider the application needs, in one place.
 *
 * Tests mount this with their own client rather than reproducing the
 * provider stack, so a provider added here (theme, auth session, error
 * boundary) reaches the tests without every test file being edited.
 *
 * `ApolloProvider` is imported from `@apollo/client/react`: in Apollo Client
 * v4 the React bindings live behind their own entry point, and the package
 * root exports only the core.
 */
export interface AppProvidersProps {
  children: ReactNode
  /** Overridden in tests; the application always uses the real client. */
  client?: ApolloClient
}

export function AppProviders({ children, client = apolloClient }: AppProvidersProps) {
  return <ApolloProvider client={client}>{children}</ApolloProvider>
}
