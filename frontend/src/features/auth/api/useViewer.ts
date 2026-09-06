import { useCallback } from 'react'
import { useQuery } from '@apollo/client/react'

import { MeDocument } from './documents'
import type { Viewer } from './types'

/**
 * What a failed `me` request is allowed to say.
 *
 * Fixed, not the server's or the browser's wording. "Who am I" fails for
 * exactly one reason a person can act on -- the request did not get through --
 * and the alternative messages are transport internals that read as bugs.
 */
const VIEWER_UNAVAILABLE = 'Could not reach Vector. Check your connection.'

export interface UseViewerResult {
  /** The signed-in user, or null for a signed-out visitor. */
  viewer: Viewer | null
  /** True until the first answer arrives. Never true again after that. */
  isLoading: boolean
  /** Set only when the request itself failed. Signed-out is not an error. */
  errorMessage: string | null
  /** Re-ask. For the retry affordance on a failed session check. */
  refresh: () => void
}

/**
 * Who is signed in.
 *
 * The single source of that answer for the whole application: one `Me` query,
 * normalised in the Apollo cache, shared by every component that calls this.
 * There is no session context, no provider and no state duplicated beside the
 * cache -- and, importantly, nothing stored anywhere JavaScript can read. The
 * session is an HttpOnly cookie; this hook asks the server who that cookie
 * belongs to and holds the answer, not the credential.
 *
 * `null` viewer and `errorMessage` are separate results because they are
 * separate situations. `me` is nullable and null means "nobody is signed in",
 * an ordinary answer a public page renders happily. A failure means the
 * question was not answered, and a guard that treated it as "signed out"
 * would sign people out of a working session every time the network hiccuped.
 */
export function useViewer(): UseViewerResult {
  const { data, error, loading, refetch } = useQuery(MeDocument)

  const refresh = useCallback(() => {
    // The rejection is already reported through `error` on the next render,
    // so the duplicate carries nothing and would surface as an unhandled
    // rejection in the console.
    void refetch().catch(() => undefined)
  }, [refetch])

  return {
    viewer: data?.me ?? null,
    // `loading` is true again during a `refetch`; the guards must not flash
    // a spinner over a page they have already admitted someone to, so this
    // reports only the load that has no answer yet.
    isLoading: loading && data === undefined,
    errorMessage: error === undefined ? null : VIEWER_UNAVAILABLE,
    refresh,
  }
}
