import { describeError } from '../lib/errors'
import type { IssueDetailFields, IssueValidationError } from './types'

/**
 * Nothing came back that this code knows how to interpret. Distinct from a
 * validation rejection and from a transport failure, and rare enough that a
 * bespoke sentence would be more alarming than useful.
 */
const UNEXPECTED_RESPONSE = 'The change could not be saved. Please try again.'

/**
 * What writing an issue can do, as three cases that cannot be confused.
 *
 * The two failure cases are genuinely different things and the backend
 * returns them through genuinely different channels:
 *
 *   - `rejected` is the payload's `errors`, a typed list arriving inside
 *     `data` over a successful response. It means the input was wrong, and
 *     every entry names the field it is about. The UI shows these against
 *     the control that produced them.
 *   - `failed` is a rejected promise: a network failure, an outage, a bug.
 *     No field owns it, so the UI shows it once, at the top.
 *
 * A discriminated union rather than `{ ok, errors, message }` so that the
 * caller cannot read `errors` out of a success or forget to check one of the
 * two failure paths -- the compiler makes it check.
 */
export type IssueSaveOutcome =
  | { status: 'saved'; issue: IssueDetailFields }
  | { status: 'rejected'; errors: readonly IssueValidationError[] }
  | { status: 'failed'; message: string }

/**
 * Every issue-returning payload in this schema, structurally.
 *
 * `IssueCreatePayload`, `IssueUpdatePayload`, `IssueSetProjectPayload` and
 * `IssueSetCyclePayload` are four generated types with identical shape, and
 * writing this as a structural parameter rather than a union of the four is
 * what keeps a fifth from needing a change here.
 */
export interface IssuePayload {
  issue: IssueDetailFields | null
  errors: readonly IssueValidationError[]
}

/**
 * Read one payload the same way every time.
 *
 * `errors` is checked before `issue`, because the backend's contract is that
 * exactly one of the two is populated and the errors are the more specific
 * answer.
 */
export function interpretPayload(
  payload: IssuePayload | undefined,
): IssueSaveOutcome {
  if (payload === undefined) {
    return { status: 'failed', message: UNEXPECTED_RESPONSE }
  }

  if (payload.errors.length > 0) {
    return { status: 'rejected', errors: payload.errors }
  }

  if (payload.issue === null) {
    return { status: 'failed', message: UNEXPECTED_RESPONSE }
  }

  return { status: 'saved', issue: payload.issue }
}

/**
 * The other channel: a rejected promise.
 *
 * The default `errorPolicy` of `none` makes `mutate` reject on a top-level
 * GraphQL error as well as on a transport failure, so this is the only place
 * either can arrive. Returned as an outcome rather than re-thrown: a failed
 * write is something the UI renders, not an exception that should take the
 * page down.
 */
export function failureOutcome(reason: unknown): IssueSaveOutcome {
  return { status: 'failed', message: describeError(reason) }
}

/**
 * Validation errors, grouped by the field they name.
 *
 * A `Map` keyed by the server's own field name, so a control asks for its own
 * errors by the name the API uses and no translation table has to be kept in
 * step. An error naming a field no control renders -- which would mean the
 * backend's contract moved -- is not dropped: ../components/IssueInspector
 * shows whatever it cannot place at the top of the panel.
 */
export function groupByField(
  errors: readonly IssueValidationError[],
): ReadonlyMap<string, string[]> {
  const grouped = new Map<string, string[]>()

  for (const error of errors) {
    const existing = grouped.get(error.field)

    if (existing === undefined) {
      grouped.set(error.field, [error.message])
    } else {
      existing.push(error.message)
    }
  }

  return grouped
}
