import { useCallback } from 'react'
import { useMutation } from '@apollo/client/react'

import { useWorkspaceSlug } from '../../../app/routes'
import { readPayload } from '../../../lib/graphql'
import type { PayloadOutcome } from '../../../lib/graphql'
import { describeError } from '../../issues/lib/errors'
import { EnvironmentCreateDocument } from './documents'
import type {
  Environment,
  EnvironmentDraft,
  EnvironmentValidationError,
} from './types'

/**
 * What an environment write can do, as three cases that cannot be confused.
 *
 * See `src/lib/graphql/payload.ts` for the reader and for why `rejected` and
 * `failed` are not one case.
 */
export type EnvironmentOutcome<T> = PayloadOutcome<T, EnvironmentValidationError>

export interface UseEnvironmentActionsResult {
  createEnvironment: (
    draft: EnvironmentDraft,
  ) => Promise<EnvironmentOutcome<Environment>>
  isSaving: boolean
}

/**
 * The only write this screen makes.
 *
 * `environmentCreate` is the whole of the schema's environment mutations:
 * there is no `environmentUpdate` and no `environmentDelete`. That is a real
 * ceiling and not an omission this file can paper over -- a target cannot be
 * renamed or retired through the API, and migration 024's
 * `releases_environment_fk` is ON DELETE RESTRICT anyway, so removing one that
 * has ever been deployed to would have to decide what happens to that history.
 * The screen says so rather than offering controls that would fail.
 *
 * Refetches the list. `environments` is a plain list rather than a connection,
 * so the created row is not normalised into it -- Apollo has no way to know
 * this new `Environment` belongs in that array -- and reproducing the server's
 * `ORDER BY name, id` in a `cache.modify` would be a second, drifting copy of
 * an ordering the server already owns.
 */
export function useEnvironmentActions(): UseEnvironmentActionsResult {
  const workspaceSlug = useWorkspaceSlug()

  const [create, createState] = useMutation(EnvironmentCreateDocument, {
    refetchQueries: ['EnvironmentList'],
  })

  const createEnvironment = useCallback(
    async (draft: EnvironmentDraft) => {
      try {
        const result = await create({ variables: { input: { workspaceSlug, ...draft } } })

        return readPayload(
          result.data?.environmentCreate,
          result.data?.environmentCreate.environment,
        )
      } catch (reason) {
        // The default `errorPolicy` of `none` makes `mutate` reject on a
        // top-level GraphQL error as well as on a transport failure.
        return { status: 'failed' as const, message: describeError(reason) }
      }
    },
    [create, workspaceSlug],
  )

  return { createEnvironment, isSaving: createState.loading }
}
