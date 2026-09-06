import { useCallback, useId, useRef, useState } from 'react'
import type { FormEvent } from 'react'

import { Button, cx, Input, Kbd, Select, Textarea } from '../../../components'
import { useCreateIssue } from '../api'
import type { IssueDetailFields, IssueValidationError } from '../api'
import styles from '../issues.module.css'
import { describePriority, PRIORITY_VALUES } from '../lib/priority'

/**
 * The longest title `app/services/issues.py` will accept.
 *
 * Mirrored here for the counter only. The input is deliberately *not* given a
 * `maxLength`, because a native maxLength would make the server's `TOO_LONG`
 * rule unreachable and the form would be silently enforcing a rule it merely
 * believes in. The server stays the authority; this number only decides when
 * the counter turns red.
 */
const TITLE_MAX_LENGTH = 500

interface FieldErrors {
  title: string[]
  priority: string[]
  other: string[]
}

const NO_FIELD_ERRORS: FieldErrors = { title: [], priority: [], other: [] }

/**
 * Sort `IssueCreatePayload.errors` into the controls that can show them.
 *
 * The service collects every violation before raising, so more than one can
 * arrive at once and all of them are kept. An error naming a field this form
 * does not render -- which would mean the backend's contract moved -- is not
 * dropped on the floor; it goes to `other` and is shown at the top of the
 * form, where it is at least visible enough to be reported.
 */
function groupErrors(errors: readonly IssueValidationError[]): FieldErrors {
  const grouped: FieldErrors = { title: [], priority: [], other: [] }

  for (const error of errors) {
    if (error.field === 'title') {
      grouped.title.push(error.message)
    } else if (error.field === 'priority') {
      grouped.priority.push(error.message)
    } else {
      grouped.other.push(`${error.field}: ${error.message}`)
    }
  }

  return grouped
}

export interface IssueComposerProps {
  onCancel: () => void
  onCreated: (issue: IssueDetailFields) => void
}

/**
 * Create an issue.
 *
 * Three inputs, because the mutation takes three arguments. There is no team
 * picker, project picker, assignee picker or status picker: `IssueCreateInput`
 * is `{ title, description, priority }` and a control for anything else would
 * be a control that cannot be submitted.
 *
 * Both of the backend's failure channels are rendered, and they land in
 * different places -- per-field messages next to the field that caused them,
 * and a single banner for a transport or server failure that no field owns.
 */
export function IssueComposer({ onCancel, onCreated }: IssueComposerProps) {
  const { createIssue, isSubmitting } = useCreateIssue()

  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [priority, setPriority] = useState(0)
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>(NO_FIELD_ERRORS)
  const [formError, setFormError] = useState<string | null>(null)

  const titleRef = useRef<HTMLInputElement>(null)
  const priorityRef = useRef<HTMLSelectElement>(null)

  // `useId` rather than hardcoded ids: two composers on one page (or one in a
  // test rendered twice) would otherwise produce duplicate ids and the
  // `aria-describedby` wiring would point at the wrong element.
  const baseId = useId()
  const titleId = `${baseId}-title`
  const titleErrorId = `${baseId}-title-error`
  const titleCountId = `${baseId}-title-count`
  const descriptionId = `${baseId}-description`
  const priorityId = `${baseId}-priority`
  const priorityErrorId = `${baseId}-priority-error`
  const priorityHintId = `${baseId}-priority-hint`

  const submit = useCallback(async () => {
    setFieldErrors(NO_FIELD_ERRORS)
    setFormError(null)

    const outcome = await createIssue({
      // Sent exactly as typed. `_validate_create` states that the title is
      // "validated as supplied -- never trimmed or rewritten", so trimming
      // here would mean the client and the server disagree about what was
      // submitted: a title of three spaces would be rejected as REQUIRED for
      // a value the user can see in the box.
      title,
      // Whitespace-only means "no description", which is a presentation
      // decision about an empty box rather than a rewrite of content: a
      // description with any real text is sent unchanged.
      description: description.trim().length === 0 ? null : description,
      priority,
    })

    if (outcome.status === 'saved') {
      onCreated(outcome.issue)
      return
    }

    if (outcome.status === 'failed') {
      setFormError(outcome.message)
      return
    }

    const grouped = groupErrors(outcome.errors)
    setFieldErrors(grouped)

    // Move focus to the first field the server rejected. Without this, a
    // submit from the keyboard leaves focus on the button and the messages
    // that just appeared above it are never announced.
    if (grouped.title.length > 0) {
      titleRef.current?.focus()
    } else if (grouped.priority.length > 0) {
      priorityRef.current?.focus()
    }
  }, [createIssue, description, onCreated, priority, title])

  const handleSubmit = useCallback(
    (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault()
      // The handler itself stays synchronous: an async function passed
      // straight to `onSubmit` returns a promise React will not await, and
      // any rejection inside it becomes an unhandled rejection.
      void submit()
    },
    [submit],
  )

  const isTitleOverLength = title.length > TITLE_MAX_LENGTH
  const hasTitleError = fieldErrors.title.length > 0
  const hasPriorityError = fieldErrors.priority.length > 0

  return (
    <form className={styles.composer} onSubmit={handleSubmit} noValidate>
      <h2 className={styles.composerHeading}>New issue</h2>

      {formError !== null && (
        <p className={styles.formError} role="alert">
          {formError}
        </p>
      )}

      {fieldErrors.other.length > 0 && (
        <p className={styles.formError} role="alert">
          {fieldErrors.other.join(' ')}
        </p>
      )}

      <div className={styles.field}>
        <label className={styles.fieldLabel} htmlFor={titleId}>
          Title
        </label>
        <Input
          aria-describedby={`${titleCountId}${hasTitleError ? ` ${titleErrorId}` : ''}`}
          autoFocus
          id={titleId}
          invalid={hasTitleError}
          onChange={(event) => {
            setTitle(event.target.value)
          }}
          ref={titleRef}
          value={title}
        />
        <span
          className={cx(styles.charCount, isTitleOverLength && styles.charCountOver)}
          id={titleCountId}
        >
          {title.length} / {TITLE_MAX_LENGTH}
        </span>
        {hasTitleError && (
          <span className={styles.fieldError} id={titleErrorId}>
            {fieldErrors.title.join(' ')}
          </span>
        )}
      </div>

      <div className={styles.field}>
        <label className={styles.fieldLabel} htmlFor={descriptionId}>
          Description <span className={styles.fieldHint}>(optional)</span>
        </label>
        <Textarea
          id={descriptionId}
          onChange={(event) => {
            setDescription(event.target.value)
          }}
          value={description}
        />
      </div>

      <div className={styles.field}>
        <label className={styles.fieldLabel} htmlFor={priorityId}>
          Priority
        </label>
        <Select
          aria-describedby={`${priorityHintId}${hasPriorityError ? ` ${priorityErrorId}` : ''}`}
          id={priorityId}
          invalid={hasPriorityError}
          onChange={(event) => {
            setPriority(Number(event.target.value))
          }}
          ref={priorityRef}
          value={priority}
        >
          {PRIORITY_VALUES.map((value) => {
            const { name } = describePriority(value)

            return (
              <option key={value} value={value}>
                {name === null ? value : `${value} - ${name}`}
              </option>
            )
          })}
        </Select>
        <span className={styles.fieldHint} id={priorityHintId}>
          The API stores priority as an integer 0-4 and attaches no names to
          it. The names above are this app&apos;s convention.
        </span>
        {hasPriorityError && (
          <span className={styles.fieldError} id={priorityErrorId}>
            {fieldErrors.priority.join(' ')}
          </span>
        )}
      </div>

      <div className={styles.composerActions}>
        <Button variant="primary" type="submit" disabled={isSubmitting}>
          {isSubmitting ? 'Creating...' : 'Create issue'}
        </Button>
        <Button onClick={onCancel} disabled={isSubmitting}>
          Cancel
        </Button>
        <span className={styles.fieldHint}>
          Press <Kbd>Esc</Kbd> to close
        </span>
      </div>
    </form>
  )
}
