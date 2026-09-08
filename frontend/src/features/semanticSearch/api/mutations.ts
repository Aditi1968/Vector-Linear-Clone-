import { useCallback, useState } from 'react'
import { useMutation } from '@apollo/client/react'

import { useWorkspaceSlug } from '../../../app/routes'
import { describeError } from '../../issues/lib/errors'
import { EmbeddingsRefreshDocument } from './documents'

export interface UseEmbeddingRefreshResult {
  /** How many embeddings the last sweep wrote. Null until one has run. */
  written: number | null
  errorMessage: string | null
  isRefreshing: boolean
  refresh: () => void
}

/**
 * Embed a batch of this workspace's issues that need it.
 *
 * ## Why a screen offers this at all
 *
 * A deployment may run no background worker -- `EMBEDDING_WORKER_ENABLED` is
 * off by default -- and then this mutation is the ONLY thing in the system
 * that ever writes an embedding. Migration 028 opens by naming exactly that
 * failure: "`embeddingsRefresh` is a GraphQL mutation somebody has to call.
 * Nobody calls it. So a workspace's duplicate suggestions say 'no duplicates
 * found' when the truth is 'no index'."
 *
 * ## It is a batch, not a job
 *
 * The server embeds up to its own limit and returns how many rows it wrote, so
 * a backlog larger than one batch needs the button pressed again. The screen
 * reports the number rather than implying completion, and the index counts
 * beside it are what actually says whether there is more to do.
 *
 * `embeddingsRefresh` returns a bare `Int!` and not a payload, so there is no
 * `errors` list and no `readPayload` here: a refusal arrives only as a
 * rejected promise.
 */
export function useEmbeddingRefresh(): UseEmbeddingRefreshResult {
  const workspaceSlug = useWorkspaceSlug()
  const [written, setWritten] = useState<number | null>(null)
  const [errorMessage, setErrorMessage] = useState<string | null>(null)

  const [refreshMutation, state] = useMutation(EmbeddingsRefreshDocument, {
    // The counts are the whole reason anybody pressed this, and the mutation
    // returns a number rather than the state -- so nothing normalises.
    refetchQueries: ['EmbeddingIndexState'],
  })

  const refresh = useCallback(() => {
    setErrorMessage(null)

    void refreshMutation({ variables: { workspaceSlug } })
      .then((result) => {
        setWritten(result.data?.embeddingsRefresh ?? null)
      })
      .catch((reason: unknown) => {
        setWritten(null)
        setErrorMessage(describeError(reason))
      })
  }, [refreshMutation, workspaceSlug])

  return { written, errorMessage, isRefreshing: state.loading, refresh }
}
