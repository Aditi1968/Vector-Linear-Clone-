/**
 * Reading one mutation payload the same way every time.
 *
 * Every mutation in this schema answers with `{ <thing>, errors }` and the
 * contract that exactly one of the two is populated. That shape is the
 * schema's, not any one feature's, which is why the reader lives here beside
 * the cache policy rather than inside whichever feature happened to need it
 * first. Four features in the second wave each carried a private copy marked
 * `ponytail:`; this is the home those notes pointed at.
 *
 * ## Three outcomes, not two
 *
 * The channel that is easy to forget exists is `rejected`: it arrives inside
 * `data` over an HTTP 200 with no GraphQL `errors` array, and it names a
 * `field` the caller can put a message beside. `failed` is a rejected promise
 * -- a transport failure, or a top-level GraphQL error under the default
 * `errorPolicy: 'none'` -- which no field owns and which a form can only
 * report as a sentence.
 *
 * Collapsing the two would either put an outage's message under a text box or
 * lose the field a refusal named. They stay separate.
 */

/** What every payload read here can produce. */
export type PayloadOutcome<TValue, TError> =
  | { status: 'ok'; value: TValue }
  | { status: 'rejected'; errors: readonly TError[] }
  | { status: 'failed'; message: string }

/**
 * What a caller shows when the server answered something no field describes.
 *
 * One string rather than a per-feature one, because it is said in exactly the
 * situation where the product has nothing specific to say. A feature whose
 * failure genuinely reads differently ("The issue could not be moved") passes
 * its own message; `features/board` and `features/issues` both do.
 */
export const UNEXPECTED_RESPONSE = 'That did not save. Please try again.'

/**
 * Turn one payload into an outcome.
 *
 * `value` is passed separately rather than read off the payload by key: the
 * field holding the thing is named differently by nearly every mutation --
 * `issue`, `template`, `savedView`, `deletedFavoriteId`, `id` -- and a reader
 * that guessed the key would be a second, private description of the schema.
 * The caller names it, and the compiler checks it exists.
 */
export function readPayload<TValue, TError>(
  payload: { errors: readonly TError[] } | undefined,
  value: TValue | null | undefined,
  unexpectedMessage: string = UNEXPECTED_RESPONSE,
): PayloadOutcome<TValue, TError> {
  if (payload === undefined) {
    return { status: 'failed', message: unexpectedMessage }
  }

  // Checked first: the backend's contract is that exactly one of the two is
  // populated, and the errors are the more specific answer.
  if (payload.errors.length > 0) {
    return { status: 'rejected', errors: payload.errors }
  }

  if (value === null || value === undefined) {
    return { status: 'failed', message: unexpectedMessage }
  }

  return { status: 'ok', value }
}
