/**
 * Turning a thrown value into something a person can read.
 *
 * Every message this returns is one the backend chose to make public or one
 * the browser produced about the transport. The backend already refuses to
 * leak internals -- `app/graphql/queries/issues.py` re-raises expected
 * pagination failures as a fixed "Invalid pagination arguments" and lets
 * everything else through GraphQL's normal error path with `from None` so no
 * parser detail rides along -- so what arrives here is safe to show.
 *
 * Structural rather than `instanceof Error`, because Apollo Client v4 types
 * what a query rejects with as `ErrorLike` -- an object with `name` and
 * `message` -- and not as `Error`. Its own error classes do extend `Error`
 * today, but the type is the contract, and matching the contract costs three
 * lines.
 *
 * The fallback exists because a rejected promise can carry anything at all,
 * and `String(someObject)` renders "[object Object]" at the user. A sentence
 * that says nothing useful is better than a sentence that says nothing
 * useful *and* looks like a bug.
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
