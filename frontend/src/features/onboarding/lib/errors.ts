/**
 * The two failure channels every one of these mutations has, and how a form
 * shows them.
 *
 * They are genuinely different things arriving over genuinely different
 * transports, and a form that reads one and not the other either swallows
 * field feedback or reports a database outage as a bad slug:
 *
 *   - `payload.errors` is *expected input* being rejected. It arrives inside
 *     `data`, over an HTTP 200, and every entry names the field it is about.
 *     `groupFieldErrors` sorts those to the controls that can show them.
 *   - A rejected promise is everything else -- an outage, a bug, a transport
 *     failure, and also the two top-level refusals this feature can provoke
 *     (`UNAUTHENTICATED`, and the deliberately ambiguous "Workspace not
 *     found" that covers "no such slug", "not a member" and "not an admin"
 *     alike). No field owns any of those, so they are shown once, at the top.
 */

/** One entry of a payload's `errors`. */
export interface FieldError {
  field: string
  code: string
  message: string
}

/**
 * Messages per field, plus everything that named a field the form has no
 * control for.
 *
 * Nothing is dropped. An error naming an unknown field means the backend's
 * contract moved, and silently discarding it is how that goes unnoticed for
 * a release; it goes to `other`, which the forms render at the top.
 */
export interface GroupedErrors {
  byField: Record<string, string[]>
  other: string[]
}

export function groupFieldErrors(
  errors: readonly FieldError[],
  knownFields: readonly string[],
): GroupedErrors {
  const byField: Record<string, string[]> = {}
  const other: string[] = []

  for (const error of errors) {
    if (knownFields.includes(error.field)) {
      ;(byField[error.field] ??= []).push(error.message)
    } else {
      other.push(`${error.field}: ${error.message}`)
    }
  }

  return { byField, other }
}

/** Whether any entry carries a given code, whatever field it named. */
export function hasCode(errors: readonly FieldError[], code: string): boolean {
  return errors.some((error) => error.code === code)
}

/**
 * A thrown value, as a sentence.
 *
 * Structural rather than `instanceof Error`: Apollo Client v4 types what a
 * mutation rejects with as `ErrorLike` -- `{ name, message }` -- not as
 * `Error`, and the type is the contract. Every message this can return is
 * one the backend chose to make public (it masks anything unexpected behind
 * a fixed string) or one the browser produced about the transport, so it is
 * safe to show.
 *
 * This is a near-copy of `features/issues/lib/errors.ts`. Deliberately
 * copied rather than imported: reaching across into another feature's
 * internals to save ten lines is the coupling that makes a feature
 * un-deletable. It wants to live in `src/lib/`, which belongs to another
 * owner -- flagged here rather than moved.
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
