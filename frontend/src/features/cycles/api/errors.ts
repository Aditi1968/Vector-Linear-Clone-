/**
 * Turning a thrown value into something a person can read.
 *
 * Structural rather than `instanceof Error`, because Apollo Client v4 types
 * what a query rejects with as `ErrorLike` -- an object with `name` and
 * `message` -- and not as `Error`. Its own error classes do extend `Error`
 * today, but the type is the contract.
 *
 * The fallback exists because a rejected promise can carry anything at all,
 * and `String(someObject)` renders "[object Object]" at the user.
 *
 * What arrives here is safe to show: the backend keeps internals out of its
 * public messages, re-raising expected failures as fixed strings and letting
 * everything else through GraphQL's normal error path `from None`.
 */

const FALLBACK_MESSAGE = 'Something went wrong. Please try again.'

export function describeError(reason: unknown): string {
  if (typeof reason === 'object' && reason !== null && 'message' in reason) {
    const { message } = reason

    if (typeof message === 'string' && message.trim().length > 0) {
      return message
    }
  }

  return FALLBACK_MESSAGE
}
