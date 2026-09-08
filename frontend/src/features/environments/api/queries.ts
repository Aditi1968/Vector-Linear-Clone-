import { useCallback } from 'react'
import { useQuery } from '@apollo/client/react'

import { useWorkspaceSlug } from '../../../app/routes'
import { describeError } from '../../issues/lib/errors'
import { EnvironmentListDocument } from './documents'
import type { Environment } from './types'

/** A stable identity for "nothing yet", so memoised children are not defeated. */
const NO_ENVIRONMENTS: readonly Environment[] = []

export interface UseEnvironmentListResult {
  environments: readonly Environment[]
  isLoading: boolean
  errorMessage: string | null
  retry: () => void
}

/**
 * Every deploy target this workspace has declared, by name.
 *
 * No pagination, no cursor and no "load more", because `environments` returns
 * `[Environment!]!` -- a plain list. A hook that offered `hasNextPage: false`
 * would be describing a connection the schema does not have.
 *
 * Read by three screens: this feature's own, the releases list (to name the
 * environment a release went to) and the release composer (to choose one).
 * All three mount the same document with the same variables, so the second and
 * third are answered from the cache.
 */
export function useEnvironmentList(): UseEnvironmentListResult {
  const workspaceSlug = useWorkspaceSlug()

  const { data, error, loading, refetch } = useQuery(EnvironmentListDocument, {
    variables: { workspaceSlug },
  })

  const retry = useCallback(() => {
    // Swallowed on purpose: `refetch` rejects *and* sets `error` on the hook
    // result, and `error` is what the screen renders.
    void refetch().catch(() => undefined)
  }, [refetch])

  return {
    environments: data?.environments ?? NO_ENVIRONMENTS,
    isLoading: loading,
    errorMessage: error === undefined ? null : describeError(error),
    retry,
  }
}
