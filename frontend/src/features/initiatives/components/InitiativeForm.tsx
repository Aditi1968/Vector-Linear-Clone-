import { useId, useMemo, useState } from 'react'
import type { FormEvent } from 'react'

import { Button, Input, Select, Textarea } from '../../../components'
import { memberLabel } from '../../issues/api'
import type { WorkspaceMember } from '../../issues/api'
import type { InitiativeDraft, InitiativeStatus, InitiativeValidationError } from '../api'
import { INITIATIVE_STATUSES, initiativeStatusLabel } from '../lib/initiatives'
import styles from '../initiatives.module.css'

export interface InitiativeFormProps {
  initialName?: string
  initialDescription?: string | null
  initialStatus?: InitiativeStatus
  initialTargetDate?: string | null
  initialOwnerId?: string | null
  members: readonly WorkspaceMember[]
  submitLabel: string
  isSaving: boolean
  errors: readonly InitiativeValidationError[]
  errorMessage: string | null
  onCancel: () => void
  onSubmit: (draft: InitiativeDraft) => void
}

/**
 * The initiative composer and editor, one component for both.
 *
 * ## Why there is no health control
 *
 * `InitiativeCreateInput` and `InitiativeUpdateInput` have no `health` field,
 * and that is the product speaking rather than an omission: migration 022
 * keeps `initiatives.health` as the health the LAST POSTED UPDATE reported,
 * denormalised onto the row so a list can draw it without reading every
 * update. The only way to change it is `initiativeUpdatePost` -- a health and
 * the sentence explaining it, together. A picker here would be a way to claim
 * a project is on track without saying why, which is the one thing the schema
 * is shaped to prevent.
 *
 * ## Why the date is a native input
 *
 * `targetDate` is the `Date` scalar: `YYYY-MM-DD`, a calendar day every
 * viewer agrees on with no time and no zone. `<input type="date">` produces
 * exactly that string, validates it in the browser, and comes with the
 * platform's own picker and keyboard handling. A component would have to
 * reproduce all of it to arrive at the same five characters.
 */
export function InitiativeForm({
  initialName = '',
  initialDescription = null,
  initialStatus = 'PLANNED',
  initialTargetDate = null,
  initialOwnerId = null,
  members,
  submitLabel,
  isSaving,
  errors,
  errorMessage,
  onCancel,
  onSubmit,
}: InitiativeFormProps) {
  const fieldId = useId()

  const [name, setName] = useState(initialName)
  const [description, setDescription] = useState(initialDescription ?? '')
  const [status, setStatus] = useState<InitiativeStatus>(initialStatus)
  const [targetDate, setTargetDate] = useState(initialTargetDate ?? '')
  const [ownerId, setOwnerId] = useState(initialOwnerId ?? '')

  /** Errors the server named, by the field it named them on. */
  const errorByField = useMemo(() => {
    const map = new Map<string, string>()

    for (const entry of errors) {
      if (!map.has(entry.field)) {
        map.set(entry.field, entry.message)
      }
    }

    return map
  }, [errors])

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()

    onSubmit({
      name: name.trim(),
      /*
        An emptied box is `null`, not `''`. "This initiative has no
        description" and "its description is the empty string" are different
        rows, and only the first is a thing anybody meant.
      */
      description: description.trim() === '' ? null : description.trim(),
      status,
      targetDate: targetDate === '' ? null : targetDate,
      ownerId: ownerId === '' ? null : ownerId,
    })
  }

  const nameError = errorByField.get('name')
  const targetDateError = errorByField.get('targetDate')
  const ownerError = errorByField.get('ownerId')

  return (
    <form className={styles.form} noValidate onSubmit={handleSubmit}>
      {errorMessage !== null && (
        <p className={styles.formError} role="alert">
          {errorMessage}
        </p>
      )}

      <div className={styles.field}>
        <label className={styles.label} htmlFor={`${fieldId}-name`}>
          Initiative name
        </label>
        <Input
          aria-describedby={nameError === undefined ? undefined : `${fieldId}-name-error`}
          aria-invalid={nameError === undefined ? undefined : true}
          id={`${fieldId}-name`}
          onChange={(event) => {
            setName(event.target.value)
          }}
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
        <label className={styles.label} htmlFor={`${fieldId}-description`}>
          Description
        </label>
        <Textarea
          id={`${fieldId}-description`}
          onChange={(event) => {
            setDescription(event.target.value)
          }}
          rows={3}
          value={description}
        />
      </div>

      <div className={styles.fieldRow}>
        <div className={styles.field}>
          <label className={styles.label} htmlFor={`${fieldId}-status`}>
            Status
          </label>
          <Select
            id={`${fieldId}-status`}
            onChange={(event) => {
              setStatus(event.target.value as InitiativeStatus)
            }}
            value={status}
          >
            {INITIATIVE_STATUSES.map((value) => (
              <option key={value} value={value}>
                {initiativeStatusLabel(value)}
              </option>
            ))}
          </Select>
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={`${fieldId}-target`}>
            Target date
          </label>
          <Input
            aria-describedby={
              targetDateError === undefined ? undefined : `${fieldId}-target-error`
            }
            aria-invalid={targetDateError === undefined ? undefined : true}
            id={`${fieldId}-target`}
            onChange={(event) => {
              setTargetDate(event.target.value)
            }}
            type="date"
            value={targetDate}
          />
          {targetDateError !== undefined && (
            <p className={styles.fieldError} id={`${fieldId}-target-error`}>
              {targetDateError}
            </p>
          )}
        </div>
      </div>

      <div className={styles.field}>
        <label className={styles.label} htmlFor={`${fieldId}-owner`}>
          Owner
        </label>
        <Select
          aria-describedby={ownerError === undefined ? undefined : `${fieldId}-owner-error`}
          aria-invalid={ownerError === undefined ? undefined : true}
          id={`${fieldId}-owner`}
          onChange={(event) => {
            setOwnerId(event.target.value)
          }}
          value={ownerId}
        >
          {/* An initiative nobody owns yet is an ordinary state, not a gap. */}
          <option value="">Nobody yet</option>
          {members.map((member) => (
            <option key={member.userId} value={member.userId}>
              {memberLabel(member)}
            </option>
          ))}
        </Select>
        {ownerError !== undefined && (
          <p className={styles.fieldError} id={`${fieldId}-owner-error`}>
            {ownerError}
          </p>
        )}
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
