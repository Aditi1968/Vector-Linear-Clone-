import { useCallback } from 'react'
import { useQuery } from '@apollo/client/react'

import { useWorkspaceSlug } from '../../../app/routes'
import { describeError } from '../../issues/lib/errors'
import { EmbeddingIndexStateDocument, SemanticIssueMatchesDocument } from './documents'
import type { IndexState, SemanticMatch } from './types'

/** A stable identity for "nothing yet", so memoised children are not defeated. */
const NO_MATCHES: readonly SemanticMatch[] = []

export interface UseSemanticMatchesResult {
  matches: readonly SemanticMatch[]
  /** Nothing typed. Distinct from "typed something that matched nothing". */
  isIdle: boolean
  isLoading: boolean
  errorMessage: string | null
  retry: () => void
}

/**
 * Issues nearest in meaning to some text.
 *
 * ## Empty is skipped, not sent
 *
 * `title` is `String!` and the service refuses a blank one, so an empty box
 * would be a request whose only possible answer is a validation error. `skip`
 * leaves `loading` false, which is what makes `isIdle` distinguishable from
 * "searching" and from "found nothing" -- three states the screen has three
 * different sentences for, and the third of which it will not say without
 * consulting the index state first.
 *
 * ## The text is passed through untouched
 *
 * Trimmed to decide whether there is a question at all, and nothing else. The
 * server embeds `title` and `description` together; normalising case,
 * stripping punctuation or splitting into terms here would be doing lexical
 * work to a string that is about to be turned into a vector, which is the one
 * transformation that cannot help it.
 */
export function useSemanticMatches(
  title: string,
  description: string,
): UseSemanticMatchesResult {
  const workspaceSlug = useWorkspaceSlug()
  const trimmedTitle = title.trim()
  const trimmedDescription = description.trim()

  const { data, error, loading, refetch } = useQuery(SemanticIssueMatchesDocument, {
    variables: {
      workspaceSlug,
      title: trimmedTitle,
      // Blank travels as null, never as `''`: the schema declares this
      // nullable and null is "there is no description", where an empty string
      // is a description that happens to be empty and would be embedded.
      description: trimmedDescription === '' ? null : trimmedDescription,
    },
    skip: trimmedTitle === '',
  })

  const retry = useCallback(() => {
    // Swallowed on purpose: `refetch` rejects *and* sets `error` on the hook
    // result, and `error` is what the screen renders.
    void refetch().catch(() => undefined)
  }, [refetch])

  return {
    matches: data?.issueDuplicateSuggestions ?? NO_MATCHES,
    isIdle: trimmedTitle === '',
    isLoading: loading,
    errorMessage: error === undefined ? null : describeError(error),
    retry,
  }
}

export interface UseIndexStateResult {
  state: IndexState | null
  isLoading: boolean
  errorMessage: string | null
  retry: () => void
}

/**
 * How much of this workspace's semantic index actually exists.
 *
 * Sent unconditionally, before anybody types, and that is the point: the
 * screen has to be able to say what it is capable of finding BEFORE it reports
 * finding nothing. Asking only after an empty result would leave the first
 * empty answer unqualified, which is exactly the answer that misleads.
 *
 * A null state is "we have not been told yet". The screen does not guess at
 * one -- it declines to say "nothing matches" while this is null, for the same
 * reason it declines to say it while `enabled` is false.
 */
export function useIndexState(): UseIndexStateResult {
  const workspaceSlug = useWorkspaceSlug()

  const { data, error, loading, refetch } = useQuery(EmbeddingIndexStateDocument, {
    variables: { workspaceSlug },
  })

  const retry = useCallback(() => {
    void refetch().catch(() => undefined)
  }, [refetch])

  return {
    state: data?.embeddingIndexingState ?? null,
    isLoading: loading,
    errorMessage: error === undefined ? null : describeError(error),
    retry,
  }
}
