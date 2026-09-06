import { useCallback, useId, useState } from 'react'

import { Button, Input } from '../../../components'
import type { CycleDraft, CycleValidationError } from '../api'
import { fromDateTimeLocal, toDateTimeLocal } from '../lib/cycles'
import styles from '../cycles.module.css'

/**
 * The one rule this form enforces before asking the server.
 *
 * `migrations/008_cycles.sql` has `CONSTRAINT cycles_dates_ordered CHECK
 * (ends_at > starts_at)`, so a zero-length or reversed cycle is refused by
 * the database. Checking it here is not a second copy of the contract -- it
 * is the only rule a form can evaluate from what is in front of it, and it
 * turns a round trip into an immediate answer.
 *
 * Everything else the server owns: the overlap rule needs the team's other
 * cycles, and a client cannot know them without asking.
 */
const DATES_OUT_OF_ORDER = 'The end must be after the start.'

export interface CycleFormProps {
  initial?: CycleDraft
  submitLabel: string
  isSaving: boolean
  onCancel: () => void
  onSubmit: (draft: CycleDraft) => void
  /** Field errors from the last rejected submission. */
  errors: readonly CycleValidationError[]
  /** A failure no field owns -- an outage, a bug. */
  errorMessage: string | null
}

/**
 * Create or edit a cycle.
 *
 * The same four fields either way, because `cycleCreate` and `cycleUpdate`
 * take the same four. `cycleUpdate` is a whole-row replace -- `number`,
 * `startsAt` and `endsAt` are all required on its input -- so the form always
 * submits every field rather than a patch.
 *
 * ## `datetime-local`, and the shift it needs
 *
 * `startsAt`/`endsAt` are the `DateTime` scalar: real instants with an
 * offset. The native control has no timezone at all -- it shows and returns a
 * *local* wall time as `YYYY-MM-DDTHH:mm` -- so the value is converted in
 * both directions by ../lib/cycles.ts. The usual bug here is
 * `toISOString().slice(0, 16)`, which hands the control UTC wall time and
 * shows a 09:00 cycle as starting at 01:00 in California.
 *
 * The native control is used rather than a picker component for the reason it
 * usually is: it is keyboard-navigable, localised and screen-reader-labelled
 * by the platform, and a JavaScript replacement is several hundred lines to
 * arrive back here.
 */
export function CycleForm({
  initial,
  submitLabel,
  isSaving,
  onCancel,
  onSubmit,
  errors,
  errorMessage,
}: CycleFormProps) {
  const [number, setNumber] = useState(initial === undefined ? '' : String(initial.number))
  const [name, setName] = useState(initial?.name ?? '')
  const [startsAt, setStartsAt] = useState(
    initial === undefined ? '' : toDateTimeLocal(initial.startsAt),
  )
  const [endsAt, setEndsAt] = useState(
    initial === undefined ? '' : toDateTimeLocal(initial.endsAt),
  )
  const [localError, setLocalError] = useState<string | null>(null)

  const fieldId = useId()
  const numberError = errors.find((error) => error.field === 'number')
  // The server reports an overlap against one or both date fields; either way
  // the message belongs beside the dates.
  const dateError = errors.find(
    (error) => error.field === 'startsAt' || error.field === 'endsAt',
  )

  const handleSubmit = useCallback(
    (event: React.FormEvent) => {
      event.preventDefault()

      const parsedNumber = Number.parseInt(number, 10)
      const start = fromDateTimeLocal(startsAt)
      const end = fromDateTimeLocal(endsAt)

      if (Number.isNaN(parsedNumber) || start === null || end === null) {
        return
      }

      if (Date.parse(end) <= Date.parse(start)) {
        setLocalError(DATES_OUT_OF_ORDER)
        return
      }

      setLocalError(null)
      onSubmit({
        number: parsedNumber,
        // An empty name is null, not `""`: the schema makes `name` nullable
        // because most cycles have none, and a blank string would store an
        // empty title where "Cycle 12" should be derived from the number.
        name: name.trim() === '' ? null : name.trim(),
        startsAt: start,
        endsAt: end,
      })
    },
    [endsAt, name, number, onSubmit, startsAt],
  )

  const dateMessage = localError ?? dateError?.message ?? null

  return (
    <form className={styles.form} onSubmit={handleSubmit} noValidate>
      {errorMessage !== null && (
        <p className={styles.formError} role="alert">
          {errorMessage}
        </p>
      )}

      <div className={styles.fieldRow}>
        <div className={styles.field}>
          <label className={styles.label} htmlFor={`${fieldId}-number`}>
            Number
          </label>
          <Input
            id={`${fieldId}-number`}
            type="number"
            min={1}
            value={number}
            autoFocus
            onChange={(event) => {
              setNumber(event.target.value)
            }}
            invalid={numberError !== undefined}
            aria-describedby={numberError === undefined ? undefined : `${fieldId}-number-error`}
            required
          />
          {numberError !== undefined && (
            <p className={styles.fieldError} id={`${fieldId}-number-error`}>
              {numberError.message}
            </p>
          )}
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={`${fieldId}-name`}>
            Name (optional)
          </label>
          <Input
            id={`${fieldId}-name`}
            value={name}
            placeholder={number === '' ? 'Cycle' : `Cycle ${number}`}
            onChange={(event) => {
              setName(event.target.value)
            }}
          />
        </div>
      </div>

      <div className={styles.fieldRow}>
        <div className={styles.field}>
          <label className={styles.label} htmlFor={`${fieldId}-starts`}>
            Starts
          </label>
          <Input
            id={`${fieldId}-starts`}
            type="datetime-local"
            value={startsAt}
            onChange={(event) => {
              setStartsAt(event.target.value)
            }}
            invalid={dateMessage !== null}
            aria-describedby={dateMessage === null ? undefined : `${fieldId}-dates-error`}
            required
          />
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={`${fieldId}-ends`}>
            Ends
          </label>
          <Input
            id={`${fieldId}-ends`}
            type="datetime-local"
            value={endsAt}
            onChange={(event) => {
              setEndsAt(event.target.value)
            }}
            invalid={dateMessage !== null}
            aria-describedby={dateMessage === null ? undefined : `${fieldId}-dates-error`}
            required
          />
        </div>
      </div>

      {dateMessage !== null && (
        <p className={styles.fieldError} id={`${fieldId}-dates-error`}>
          {dateMessage}
        </p>
      )}

      <p className={styles.hint}>
        The end is exclusive: a cycle ending at the instant the next one starts
        is how two cycles sit back to back. A team&apos;s cycles may not
        overlap.
      </p>

      <div className={styles.formActions}>
        <Button type="button" variant="secondary" onClick={onCancel}>
          Cancel
        </Button>
        <Button
          type="submit"
          variant="primary"
          loading={isSaving}
          disabled={number === '' || startsAt === '' || endsAt === ''}
        >
          {submitLabel}
        </Button>
      </div>
    </form>
  )
}
