import { describeError } from '../../issues/lib/errors'
import type { ValidationError } from './types'

/**
 * What a collaboration mutation can do, as three cases that cannot be
 * confused.
 *
 * The two failure cases are genuinely different things arriving through
 * genuinely different channels, and every mutation in this schema uses both:
 *
 *   - `rejected` is the payload's `errors` list: a typed list inside `data`,
 *     over an HTTP 200, describing input the server refused -- an empty
 *     comment body, a label name already taken, an issue related to itself.
 *     Every entry names the field it is about, so a form can put it beside
 *     the control.
 *   - `failed` is a rejected promise: a network failure, an outage, a bug, or
 *     an authorisation refusal. No field owns it, so a panel shows it once.
 *
 * A discriminated union rather than `{ ok, errors, message }` so a caller
 * cannot read `errors` out of a success or forget one of the two failure
 * paths -- the compiler makes it check.
 *
 * `payload` on the success case exists for the one flow that needs more than
 * "it worked": creating a label and then attaching it needs the id the
 * server assigned.
 */
export type MutationOutcome<TPayload> =
  | { status: 'ok'; payload: TPayload }
  | { status: 'rejected'; errors: readonly ValidationError[] }
  | { status: 'failed'; message: string }

/**
 * Nothing came back that this code knows how to interpret. Distinct from a
 * rejection and from a transport failure, and rare enough that a bespoke
 * sentence per mutation would be more alarming than useful.
 */
const UNEXPECTED_RESPONSE = 'That did not go through. Please try again.'

/**
 * Run a mutation and sort its answer into the three cases above.
 *
 * `send` returns the *payload*, not the whole result, because the payload is
 * the only part whose name differs per mutation and the caller is the only
 * one who knows it. Everything after that is identical for all seven
 * mutations in this feature, which is why it is written once.
 *
 * `undefined` from `send` means `data` was absent on a resolved response --
 * possible, and not something any panel can act on beyond saying so.
 */
export async function settle<TPayload extends { errors: readonly ValidationError[] }>(
  send: () => Promise<TPayload | undefined>,
): Promise<MutationOutcome<TPayload>> {
  try {
    const payload = await send()

    if (payload === undefined) {
      return { status: 'failed', message: UNEXPECTED_RESPONSE }
    }

    // Checked before anything else: the backend's contract is that a
    // non-empty `errors` means nothing was written, and it is the more
    // specific answer.
    if (payload.errors.length > 0) {
      return { status: 'rejected', errors: payload.errors }
    }

    return { status: 'ok', payload }
  } catch (reason) {
    // The default `errorPolicy` of `none` makes a mutation reject on a
    // top-level GraphQL error as well as on a transport failure, so this is
    // the only place either can arrive. Returned rather than re-thrown: a
    // failed mutation is an outcome a panel renders, not an exception that
    // should take the issue view down with it.
    return { status: 'failed', message: describeError(reason) }
  }
}

/**
 * The one sentence a panel shows for an outcome that is not `ok`.
 *
 * Validation entries carry their own messages and the service collects every
 * violation before raising, so all of them are joined rather than only the
 * first -- a form that reported one problem at a time would make fixing two
 * take two round trips.
 */
export function describeOutcome(outcome: MutationOutcome<unknown>): string | null {
  switch (outcome.status) {
    case 'ok':
      return null
    case 'rejected':
      return outcome.errors.map((error) => error.message).join(' ')
    case 'failed':
      return outcome.message
  }
}
