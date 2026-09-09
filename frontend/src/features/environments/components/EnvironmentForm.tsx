import { useId, useMemo, useState } from 'react'
import type { FormEvent } from 'react'

import { Button, Input, Select } from '../../../components'
import { partitionFieldErrors } from '../../../lib/graphql'
import type { EnvironmentDraft, EnvironmentKind, EnvironmentValidationError } from '../api'
import { ENVIRONMENT_KINDS, environmentKindLabel } from '../lib/environments'
import styles from '../environments.module.css'

/**
 * The fields this form draws a control for.
 *
 * Module scope so the memo below keys on a stable array. Anything the server
 * names that is not in here has nowhere to sit, and goes to the form-level
 * alert rather than being dropped -- see `partitionFieldErrors`.
 */
const FIELDS = ['name', 'kind'] as const

export interface EnvironmentFormProps {
  isSaving: boolean
  errors: readonly EnvironmentValidationError[]
  errorMessage: string | null
  onCancel: () => void
  onSubmit: (draft: EnvironmentDraft) => void
}

/**
 * Declare a deploy target: what it is called, and what kind of thing it is.
 *
 * Two fields, and the second is the one people skip past. `kind` is not
 * derived from the name and cannot be: a workspace may run "Prod EU" and "Prod
 * US" and both are production, so a rule written against the string would
 * recognise neither. It is what a future policy keys off -- an approval before
 * a production deploy, a red banner -- and it survives a rename, which the
 * name by definition does not.
 *
 * Development is the default because it is the answer that is safe to get
 * wrong. There is no database default for `kind`, deliberately, so this form
 * always sends one.
 */
export function EnvironmentForm({
  isSaving,
  errors,
  errorMessage,
  onCancel,
  onSubmit,
}: EnvironmentFormProps) {
  const fieldId = useId()

  const [name, setName] = useState('')
  const [kind, setKind] = useState<EnvironmentKind>('DEVELOPMENT')

  const { byField: errorByField, unattached } = useMemo(
    () => partitionFieldErrors(errors, FIELDS),
    [errors],
  )

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()

    onSubmit({ name: name.trim(), kind })
  }

  const nameError = errorByField.get('name')
  const kindError = errorByField.get('kind')

  // A transport failure and a refusal no control on this form owns are the
  // same thing to the reader: the server said no, and no input is at fault.
  const formMessages = errorMessage === null ? unattached : [errorMessage, ...unattached]

  return (
    <form className={styles.form} noValidate onSubmit={handleSubmit}>
      {formMessages.length > 0 && (
        <p className={styles.formError} role="alert">
          {formMessages.join(' ')}
        </p>
      )}

      <div className={styles.field}>
        <label className={styles.label} htmlFor={`${fieldId}-name`}>
          Name
        </label>
        <Input
          aria-describedby={
            nameError === undefined ? `${fieldId}-name-hint` : `${fieldId}-name-error`
          }
          aria-invalid={nameError === undefined ? undefined : true}
          id={`${fieldId}-name`}
          maxLength={100}
          onChange={(event) => {
            setName(event.target.value)
          }}
          placeholder="Production"
          required
          value={name}
        />
        {nameError !== undefined && (
          <p className={styles.fieldError} id={`${fieldId}-name-error`}>
            {nameError}
          </p>
        )}
        <p className={styles.hint} id={`${fieldId}-name-hint`}>
          Unique in this workspace. Two targets called “Production” is not a configuration
          anyone means to have — whichever one a deploy script picked would be the one
          nobody was watching.
        </p>
      </div>

      <div className={styles.field}>
        <label className={styles.label} htmlFor={`${fieldId}-kind`}>
          Kind
        </label>
        <Select
          aria-describedby={`${fieldId}-kind-hint`}
          aria-invalid={kindError === undefined ? undefined : true}
          id={`${fieldId}-kind`}
          onChange={(event) => {
            setKind(event.target.value as EnvironmentKind)
          }}
          value={kind}
        >
          {ENVIRONMENT_KINDS.map((candidate) => (
            <option key={candidate} value={candidate}>
              {environmentKindLabel(candidate)}
            </option>
          ))}
        </Select>
        {kindError !== undefined && <p className={styles.fieldError}>{kindError}</p>}
        <p className={styles.hint} id={`${fieldId}-kind-hint`}>
          What it is, as opposed to what it is called. Several targets may be production;
          the kind is what a rule reads, and it survives a rename.
        </p>
      </div>

      <div className={styles.formActions}>
        <Button onClick={onCancel} type="button" variant="ghost">
          Cancel
        </Button>
        <Button disabled={isSaving || name.trim() === ''} type="submit" variant="primary">
          Add environment
        </Button>
      </div>
    </form>
  )
}
