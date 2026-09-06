import { useCallback } from 'react'
import { useMutation } from '@apollo/client/react'
import type { ApolloCache } from '@apollo/client'

import { LoginDocument, MeDocument, RegisterDocument } from './documents'
import type { AuthValidationError, Viewer } from './types'

/**
 * Nothing came back that this code knows how to read. Distinct from a
 * rejection and from a transport failure, and rare enough that a bespoke
 * sentence would alarm more than it explains.
 */
const UNEXPECTED_RESPONSE = 'Something went wrong. Please try again.'

/**
 * What a sign-in attempt can do, as three cases that cannot be confused.
 *
 * `rejected` is the payload's `errors` -- typed, per-field, arriving inside
 * `data` over a 200, and the form's business to put next to the right input.
 * `failed` is a rejected promise: an outage, a bug, a dropped connection. No
 * field owns it, so it is shown once, above the form. A discriminated union
 * rather than `{ ok, errors, message }` so the compiler makes the caller
 * handle both.
 */
export type SignInOutcome =
  | { status: 'signedIn'; viewer: Viewer }
  | { status: 'rejected'; errors: readonly AuthValidationError[] }
  | { status: 'failed'; message: string }

export interface UseSignInResult<TInput> {
  submit: (input: TInput) => Promise<SignInOutcome>
  isSubmitting: boolean
}

export interface LoginFields {
  email: string
  password: string
}

export interface RegisterFields extends LoginFields {
  /** Optional in the schema; sent as null when the visitor leaves it blank. */
  name: string | null
}

/**
 * Teach the cache that this is now the viewer.
 *
 * Without it, signing in normalises a `User` entity and leaves `ROOT_QUERY.me`
 * exactly as it was -- so `useViewer` would keep reporting "signed out" until
 * something refetched, and every guard would bounce the person who just
 * successfully signed in straight back to the sign-in page.
 *
 * Written inside the mutation's `update`, which runs within its cache
 * transaction: the viewer and the mutation's own result land in one broadcast
 * and the tree re-renders once.
 */
function adoptViewer(cache: ApolloCache, user: Viewer): void {
  cache.writeQuery({ query: MeDocument, data: { me: user } })
}

/**
 * Normalise a `{ user, errors }` payload into an outcome.
 *
 * Errors are checked before `user` because the backend's contract is that
 * exactly one of the two is populated, and the errors are the more specific
 * answer.
 */
function toOutcome(
  payload: { user: Viewer | null; errors: readonly AuthValidationError[] } | undefined,
): SignInOutcome {
  if (payload === undefined) {
    return { status: 'failed', message: UNEXPECTED_RESPONSE }
  }

  if (payload.errors.length > 0) {
    return { status: 'rejected', errors: payload.errors }
  }

  if (payload.user === null) {
    return { status: 'failed', message: UNEXPECTED_RESPONSE }
  }

  return { status: 'signedIn', viewer: payload.user }
}

/**
 * Exchange credentials for a session.
 *
 * There is no token here and no place to put one. The server answers with a
 * `Set-Cookie` the browser stores and JavaScript cannot read; all this hook
 * gets back is who it is now talking as. Nothing is written to
 * `localStorage`, `sessionStorage` or a JS-visible cookie -- that is the
 * property that makes an XSS unable to lift a session off this origin.
 */
export function useLogin(): UseSignInResult<LoginFields> {
  const [mutate, { loading }] = useMutation(LoginDocument, {
    update(cache, result) {
      const user = result.data?.login.user

      if (user != null) {
        adoptViewer(cache, user)
      }
    },
  })

  const submit = useCallback(
    async ({ email, password }: LoginFields): Promise<SignInOutcome> => {
      try {
        const result = await mutate({ variables: { input: { email, password } } })

        return toOutcome(result.data?.login)
      } catch {
        // `errorPolicy` defaults to `none`, so this is the only channel a
        // top-level GraphQL error or a transport failure arrives on. The
        // reason is deliberately not read: an auth screen is the last place
        // to render a message whose provenance we have not checked.
        return { status: 'failed', message: UNEXPECTED_RESPONSE }
      }
    },
    [mutate],
  )

  return { submit, isSubmitting: loading }
}

/** Create an account. Registering signs you in, so the cache write is the same. */
export function useRegister(): UseSignInResult<RegisterFields> {
  const [mutate, { loading }] = useMutation(RegisterDocument, {
    update(cache, result) {
      const user = result.data?.register.user

      if (user != null) {
        adoptViewer(cache, user)
      }
    },
  })

  const submit = useCallback(
    async ({ email, password, name }: RegisterFields): Promise<SignInOutcome> => {
      try {
        const result = await mutate({ variables: { input: { email, password, name } } })

        return toOutcome(result.data?.register)
      } catch {
        return { status: 'failed', message: UNEXPECTED_RESPONSE }
      }
    },
    [mutate],
  )

  return { submit, isSubmitting: loading }
}
