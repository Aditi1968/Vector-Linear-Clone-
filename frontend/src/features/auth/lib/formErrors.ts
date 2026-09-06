import { useCallback, useEffect, useRef, useState } from 'react'
import type { RefObject } from 'react'

import type { SignInOutcome } from '../api'

/**
 * Server-reported errors, sorted into the two places a form can put them.
 *
 * `byField` is what goes beside an input. `form` is everything that names no
 * input the form renders -- a transport failure, and `credentials`, which the
 * backend uses precisely because it refuses to say whether the email or the
 * password was wrong.
 */
export interface FormErrors {
  byField: Readonly<Record<string, string>>
  form: string | null
}

const NO_ERRORS: FormErrors = { byField: {}, form: null }

/**
 * The DOM id of a field's error text, so the input can point
 * `aria-describedby` at it. One rule, so the two cannot disagree.
 */
export function errorId(field: string): string {
  return `${field}-error`
}

/**
 * Sort an outcome into field errors and form errors.
 *
 * An error naming a field the form does not render is promoted to the form
 * rather than dropped. Dropping it is the failure mode that matters: the
 * request visibly fails, nothing appears, and the person is left retyping a
 * password against a rule they were never told about.
 *
 * Only the first error per field is kept. The backend reports at most one per
 * field today, and a stack of messages under one input is not something a
 * form can lay out sensibly anyway.
 */
function toFormErrors(
  outcome: SignInOutcome,
  fields: readonly string[],
): FormErrors {
  if (outcome.status === 'signedIn') {
    return NO_ERRORS
  }

  if (outcome.status === 'failed') {
    return { byField: {}, form: outcome.message }
  }

  const byField: Record<string, string> = {}
  const unattached: string[] = []

  for (const error of outcome.errors) {
    if (!fields.includes(error.field)) {
      unattached.push(error.message)
    } else if (!(error.field in byField)) {
      byField[error.field] = error.message
    }
  }

  return {
    byField,
    form: unattached.length === 0 ? null : unattached.join(' '),
  }
}

export interface AuthFormErrors {
  errors: FormErrors
  /** Record what the server said about the last attempt, and move focus. */
  report: (outcome: SignInOutcome) => void
  /** Put on the `<form>`; the hook searches it for the first rejected input. */
  formRef: RefObject<HTMLFormElement | null>
  /** Put on the form-level alert; focused when no single input is at fault. */
  alertRef: RefObject<HTMLDivElement | null>
}

/**
 * Hold a form's server errors and put the keyboard where the problem is.
 *
 * Moving focus is the part that is easy to skip and hardest to live without.
 * Rendering a message somewhere on the page leaves a screen-reader user with
 * a form that silently did nothing; focusing the rejected input announces its
 * label, its invalid state and -- through `aria-describedby` -- the reason,
 * and it puts a sighted keyboard user's cursor in the field they have to fix.
 *
 * The focus runs in an effect keyed on an attempt counter rather than
 * straight after the mutation, because the input is not marked
 * `aria-invalid` until React has committed the new errors. The counter is
 * what makes two identical rejections in a row still move focus.
 *
 * `fields` must be a stable array -- declare it at module scope, not inline.
 */
export function useAuthFormErrors(fields: readonly string[]): AuthFormErrors {
  const formRef = useRef<HTMLFormElement>(null)
  const alertRef = useRef<HTMLDivElement>(null)
  const [state, setState] = useState({ errors: NO_ERRORS, attempt: 0 })

  useEffect(() => {
    if (state.attempt === 0) {
      return
    }

    const invalid = formRef.current?.querySelector<HTMLElement>(
      '[aria-invalid="true"]',
    )

    ;(invalid ?? alertRef.current)?.focus()
  }, [state])

  const report = useCallback(
    (outcome: SignInOutcome) => {
      setState((previous) => ({
        errors: toFormErrors(outcome, fields),
        attempt: previous.attempt + 1,
      }))
    },
    [fields],
  )

  return { errors: state.errors, report, formRef, alertRef }
}
