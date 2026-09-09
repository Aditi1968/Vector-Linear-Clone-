import { useId, useMemo, useState } from 'react'
import type { FormEvent } from 'react'

import { Button, Checkbox, Input } from '../../../components'
import { partitionFieldErrors } from '../../../lib/graphql'
import type { LabelGroupDraft, LabelGroupValidationError } from '../api'
import { EXCLUSIVITY_HELP } from '../lib/labelGroups'
import styles from '../labelGroups.module.css'

/**
 * The fields this form draws a control for.
 *
 * Module scope so the memo below keys on a stable array. Anything the server
 * names that is not in here has nowhere to sit, and goes to the form-level
 * alert rather than being dropped -- see `partitionFieldErrors`.
 */
const FIELDS = ['name', 'exclusive'] as const

export interface LabelGroupFormProps {
  /** Seeds the fields when editing. Both are sent whether or not they changed. */
  initialName?: string
  initialExclusive?: boolean
  submitLabel: string
  isSaving: boolean
  errors: readonly LabelGroupValidationError[]
  errorMessage: string | null
  onCancel: () => void
  onSubmit: (draft: LabelGroupDraft) => void
}

/**
 * A group's two fields, both of which are always sent.
 *
 * `LabelGroupUpdateInput` declares `name: String!` and `exclusive: Boolean!`,
 * so an edit is a whole-row replace rather than a patch -- unlike
 * `InitiativeUpdateInput`, which leaves an absent field alone. Seeding both
 * from the group being edited is therefore not a convenience; a form that sent
 * only the field somebody touched would blank the other.
 *
 * ## Turning exclusivity on can be refused, and the refusal is the point
 *
 * The checkbox does not know whether the change will be accepted, and it
 * deliberately does not try to find out. Migration 021 cascades the flip down
 * onto every label in the group and then onto `issue_labels.exclusivity_key`,
 * where `issue_labels_exclusive_group_key` refuses it if any issue in the
 * workspace already wears two labels from the group -- atomically, in the same
 * statement. A client-side pre-check would need every issue's labels, would be
 * stale by the time it was answered, and would still have to handle the
 * refusal. So the form submits, and the message comes back and is shown.
 */
export function LabelGroupForm({
  initialName = '',
  initialExclusive = false,
  submitLabel,
  isSaving,
  errors,
  errorMessage,
  onCancel,
  onSubmit,
}: LabelGroupFormProps) {
  const fieldId = useId()

  const [name, setName] = useState(initialName)
  const [exclusive, setExclusive] = useState(initialExclusive)

  const { byField: errorByField, unattached } = useMemo(
    () => partitionFieldErrors(errors, FIELDS),
    [errors],
  )

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()

    onSubmit({ name: name.trim(), exclusive })
  }

  const nameError = errorByField.get('name')
  const exclusiveError = errorByField.get('exclusive')

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
        {/* "Group name" and not "Name", because the panel behind this dialog
            names labels too and two controls sharing an accessible name is a
            screen you cannot navigate by voice or by name lookup. */}
        <label className={styles.label} htmlFor={`${fieldId}-name`}>
          Group name
        </label>
        <Input
          aria-describedby={nameError === undefined ? undefined : `${fieldId}-name-error`}
          aria-invalid={nameError === undefined ? undefined : true}
          id={`${fieldId}-name`}
          maxLength={50}
          onChange={(event) => {
            setName(event.target.value)
          }}
          placeholder="Priority"
          required
          value={name}
        />
        {nameError !== undefined && (
          <p className={styles.fieldError} id={`${fieldId}-name-error`}>
            {nameError}
          </p>
        )}
      </div>

      <div className={styles.field}>
        <div className={styles.checkboxRow}>
          <Checkbox
            aria-describedby={`${fieldId}-exclusive-hint`}
            checked={exclusive}
            id={`${fieldId}-exclusive`}
            onChange={(event) => {
              setExclusive(event.target.checked)
            }}
          />
          <label className={styles.label} htmlFor={`${fieldId}-exclusive`}>
            One label at a time
          </label>
        </div>
        {exclusiveError !== undefined && (
          <p className={styles.fieldError}>{exclusiveError}</p>
        )}
        <p className={styles.hint} id={`${fieldId}-exclusive-hint`}>
          {exclusive ? EXCLUSIVITY_HELP.exclusive : EXCLUSIVITY_HELP.shared} Turning this
          on is refused while any issue already wears two labels from the group.
        </p>
      </div>

      <div className={styles.formActions}>
        <Button onClick={onCancel} type="button" variant="ghost">
          Cancel
        </Button>
        <Button disabled={isSaving || name.trim() === ''} type="submit" variant="primary">
          {submitLabel}
        </Button>
      </div>
    </form>
  )
}
