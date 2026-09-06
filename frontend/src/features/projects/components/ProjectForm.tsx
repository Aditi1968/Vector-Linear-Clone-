import { useCallback, useId, useState } from 'react'

import { Button, Input, Select, Textarea } from '../../../components'
import type { ProjectDraft, ProjectMember, ProjectState, ProjectValidationError } from '../api'
import { memberName, PROJECT_STATES, projectStateLabel } from '../lib/projects'
import styles from '../projects.module.css'

export interface ProjectFormProps {
  /** Prefills the fields. Absent when creating. */
  initial?: ProjectDraft
  /** The lead picker's options. Empty while the member query is answering. */
  members: readonly ProjectMember[]
  submitLabel: string
  isSubmitting: boolean
  onCancel: () => void
  onSubmit: (draft: ProjectDraft) => void
  /** Field errors from the last rejected submission. */
  errors: readonly ProjectValidationError[]
  /** A failure no field owns -- an outage, a bug. */
  errorMessage: string | null
}

/**
 * The one form that creates a project and the one that edits it.
 *
 * The same fields either way, because they are the same fields: `create` and
 * `update` take the same five, and a second component would be a copy that
 * drifts. What differs is the initial values and the button's word, and both
 * are props.
 *
 * ## Dates
 *
 * `<input type="date">`, not a picker component. It is keyboard-navigable,
 * localised, and screen-reader-labelled by the platform, and its value format
 * is `YYYY-MM-DD` -- which is exactly the `Date` scalar `targetDate` is. A
 * JavaScript picker would be several hundred lines to arrive back here.
 *
 * ## Validation
 *
 * Only the emptiness of the name is checked here, because it is the only rule
 * this code can know: everything else -- length limits, what a legal state
 * transition is -- belongs to the server, which returns structured `errors`
 * with a `field` naming what to fix. Reimplementing those rules client-side
 * would put a second, drifting copy of the contract in the browser.
 */
export function ProjectForm({
  initial,
  members,
  submitLabel,
  isSubmitting,
  onCancel,
  onSubmit,
  errors,
  errorMessage,
}: ProjectFormProps) {
  const [name, setName] = useState(initial?.name ?? '')
  const [description, setDescription] = useState(initial?.description ?? '')
  const [state, setState] = useState<ProjectState>(initial?.state ?? 'PLANNED')
  const [targetDate, setTargetDate] = useState(initial?.targetDate ?? '')
  const [leadId, setLeadId] = useState(initial?.leadId ?? '')

  // One base id per instance, so two forms on one page cannot produce two
  // controls with the same `id` -- which would make one label point at the
  // wrong input.
  const fieldId = useId()
  const nameError = errors.find((error) => error.field === 'name')
  const dateError = errors.find((error) => error.field === 'targetDate')

  const handleSubmit = useCallback(
    (event: React.FormEvent) => {
      event.preventDefault()

      const trimmed = name.trim()

      if (trimmed.length === 0) {
        return
      }

      // Empty strings become nulls on the way out. The API distinguishes an
      // absent value from an empty one, and `description: ""` would store a
      // blank rather than clearing the field.
      onSubmit({
        name: trimmed,
        description: description.trim() === '' ? null : description.trim(),
        state,
        targetDate: targetDate === '' ? null : targetDate,
        leadId: leadId === '' ? null : leadId,
      })
    },
    [description, leadId, name, onSubmit, state, targetDate],
  )

  return (
    <form className={styles.form} onSubmit={handleSubmit} noValidate>
      {errorMessage !== null && (
        <p className={styles.formError} role="alert">
          {errorMessage}
        </p>
      )}

      <div className={styles.field}>
        <label className={styles.label} htmlFor={`${fieldId}-name`}>
          Name
        </label>
        <Input
          id={`${fieldId}-name`}
          value={name}
          onChange={(event) => {
            setName(event.target.value)
          }}
          invalid={nameError !== undefined}
          aria-describedby={nameError === undefined ? undefined : `${fieldId}-name-error`}
          autoFocus
          required
        />
        {nameError !== undefined && (
          <p className={styles.fieldError} id={`${fieldId}-name-error`}>
            {nameError.message}
          </p>
        )}
      </div>

      <div className={styles.field}>
        <label className={styles.label} htmlFor={`${fieldId}-description`}>
          Description
        </label>
        <Textarea
          id={`${fieldId}-description`}
          rows={4}
          value={description}
          onChange={(event) => {
            setDescription(event.target.value)
          }}
        />
      </div>

      <div className={styles.fieldRow}>
        <div className={styles.field}>
          <label className={styles.label} htmlFor={`${fieldId}-state`}>
            State
          </label>
          <Select
            id={`${fieldId}-state`}
            value={state}
            onChange={(event) => {
              // The cast is the one place the union meets the DOM, which types
              // every select value as `string`. Safe because the options are
              // generated from `PROJECT_STATES`, which *is* the union.
              setState(event.target.value as ProjectState)
            }}
          >
            {PROJECT_STATES.map((option) => (
              <option key={option} value={option}>
                {projectStateLabel(option)}
              </option>
            ))}
          </Select>
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={`${fieldId}-target`}>
            Target date
          </label>
          <Input
            id={`${fieldId}-target`}
            type="date"
            value={targetDate}
            onChange={(event) => {
              setTargetDate(event.target.value)
            }}
            invalid={dateError !== undefined}
            aria-describedby={dateError === undefined ? undefined : `${fieldId}-target-error`}
          />
          {dateError !== undefined && (
            <p className={styles.fieldError} id={`${fieldId}-target-error`}>
              {dateError.message}
            </p>
          )}
        </div>
      </div>

      <div className={styles.field}>
        <label className={styles.label} htmlFor={`${fieldId}-lead`}>
          Lead
        </label>
        <Select
          id={`${fieldId}-lead`}
          value={leadId}
          onChange={(event) => {
            setLeadId(event.target.value)
          }}
        >
          {/* An explicit "nobody", because no lead is a real answer and the
              form must be able to go back to it. */}
          <option value="">No lead</option>
          {members.map((member) => (
            <option key={member.userId} value={member.userId}>
              {memberName(member)}
            </option>
          ))}
        </Select>
      </div>

      <div className={styles.formActions}>
        <Button type="button" variant="secondary" onClick={onCancel}>
          Cancel
        </Button>
        <Button
          type="submit"
          variant="primary"
          loading={isSubmitting}
          disabled={name.trim().length === 0}
        >
          {submitLabel}
        </Button>
      </div>
    </form>
  )
}
