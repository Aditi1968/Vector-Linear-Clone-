/**
 * Turning a thrown value into something a person can read.
 *
 * A copy of `features/cycles/api/errors.ts`, which is itself a copy of the
 * issues feature's -- deliberately, and for the reason those two are separate:
 * neither feature publishes it, so importing one would reach past a feature's
 * own boundary into its internals for twelve lines of pure function.
 *
 * Structural rather than `instanceof Error`, because Apollo Client v4 types
 * what a mutation rejects with as `ErrorLike` -- an object with `name` and
 * `message` -- not as `Error`.
 *
 * What arrives here is safe to show: the backend keeps internals out of its
 * public messages. The fallback exists because a rejected promise can carry
 * anything at all, and `String(someObject)` renders "[object Object]" at the
 * user.
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
