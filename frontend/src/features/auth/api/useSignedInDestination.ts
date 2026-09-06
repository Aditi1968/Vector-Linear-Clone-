import { useCallback } from 'react'
import { useQuery } from '@apollo/client/react'

import { signedInDestination } from './destination'
import { MyWorkspacesDocument } from './documents'

/** Same wording as `useViewer`: the request did not get through. */
const DESTINATION_UNAVAILABLE = 'Could not reach Vector. Check your connection.'

export interface UseSignedInDestinationResult {
  /** Where to send the viewer, once it is known. */
  destination: string | null
  /** The answer has not arrived yet. */
  isResolving: boolean
  errorMessage: string | null
  retry: () => void
}

/**
 * Where the signed-in viewer belongs, according to the server.
 *
 * `enabled` exists because `myWorkspaces` is a protected field: asked without
 * a session it answers UNAUTHENTICATED, which on a public sign-in page would
 * be an error rendered at a visitor whose only mistake was arriving. So the
 * question is only asked once `me` has said somebody is there.
 *
 * Reported as a failure rather than defaulted when the lookup breaks. The
 * two candidate defaults are both wrong in a way that is hard to notice: send
 * an existing member to onboarding and they are invited to create a duplicate
 * workspace; send a new account to a workspace list and they meet an empty
 * screen with no next step.
 */
export function useSignedInDestination(enabled: boolean): UseSignedInDestinationResult {
  const { data, error, loading, refetch } = useQuery(MyWorkspacesDocument, {
    skip: !enabled,
  })

  const retry = useCallback(() => {
    void refetch().catch(() => undefined)
  }, [refetch])

  return {
    destination: data === undefined ? null : signedInDestination(data.myWorkspaces),
    // `loading` alone would go true again on a retry and flash a spinner over
    // a destination already known; skipped queries never load at all.
    isResolving: loading && data === undefined,
    errorMessage: error === undefined ? null : DESTINATION_UNAVAILABLE,
    retry,
  }
}

export { signedInDestination, ONBOARDING_PATH } from './destination'
