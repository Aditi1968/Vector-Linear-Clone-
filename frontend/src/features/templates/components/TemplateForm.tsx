import { useId, useMemo, useState } from 'react'
import type { FormEvent } from 'react'

import { Button, Input, Select, Textarea } from '../../../components'
import { partitionFieldErrors } from '../../../lib/graphql'
import type { WorkspaceTeam } from '../../issues/api'
import { PRIORITY_VALUES, describePriority } from '../../issues/lib/priority'
import type { IssueTemplate, IssueTemplateDraft, TemplateValidationError } from '../api'
import styles from '../templates.module.css'

/**
 * The one field this form draws an error slot for.
 *
 * Module scope so the memo below keys on a stable array. A refusal naming
 * `teamId`, `title`, `priority`, `estimate` or anything else this form sends
 * without an error slot goes to the form-level alert rather than being
 * dropped -- see `partitionFieldErrors`.
 */
const FIELDS = ['name'] as const

export interface TemplateFormProps {
  /** The template being edited, or null when composing a new one. */
  template: IssueTemplate | null
  teams: readonly WorkspaceTeam[]
  submitLabel: string
  isSaving: boolean
  errors: readonly TemplateValidationError[]
  errorMessage: string | null
  onCancel: () => void
  onSubmit: (draft: IssueTemplateDraft) => void
}

/**
 * The template composer and editor.
 *
 * ## Why this form carries fields it does not show
 *
 * `IssueTemplateUpdateInput.template` is an `IssueTemplateFieldsInput!` --
 * a WHOLE-ROW REPLACE, not a patch. Every field absent from a submission
 * takes its schema default: `null` for the scalars, `[]` for `labelIds`.
 *
 * So a form that renders six of the eleven fields and submits only those does
 * not "leave the rest alone" -- it clears them. A template carrying an
 * assignee, a project, a cycle and three labels would come back from an
 * innocent rename with all six gone.
 *
 * `assigneeId`, `projectId`, `cycleId` and `labelIds` are therefore held in
 * this component from the loaded template and submitted unchanged. They have
 * no controls in this pass -- each needs a picker fed by its own list -- but
 * they are preserved rather than dropped. Deleting one of them from the
 * submitted draft is a data-loss bug, not a smaller form.
 */
export function TemplateForm({
  template,
  teams,
  submitLabel,
  isSaving,
  errors,
  errorMessage,
  onCancel,
  onSubmit,
}: TemplateFormProps) {
  const fieldId = useId()

  const [name, setName] = useState(template?.name ?? '')
  const [teamId, setTeamId] = useState(template?.teamId ?? '')
  const [title, setTitle] = useState(template?.title ?? '')
  const [description, setDescription] = useState(template?.description ?? '')
  const [priority, setPriority] = useState(
    template?.priority === null || template?.priority === undefined
      ? ''
      : String(template.priority),
  )
  const [estimate, setEstimate] = useState(
    template?.estimate === null || template?.estimate === undefined
      ? ''
      : String(template.estimate),
  )

  /** Errors the server named, by the field it named them on. */
  const { byField: errorByField, unattached } = useMemo(
    () => partitionFieldErrors(errors, FIELDS),
    [errors],
  )

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()

    onSubmit({
      name: name.trim(),
      teamId: teamId === '' ? null : teamId,
      // An empty box means "the template does not set this", which is null --
      // not an empty string, which would make every issue from this template
      // start with a blank title the server accepted.
      title: title.trim() === '' ? null : title.trim(),
      description: description.trim() === '' ? null : description.trim(),
      priority: priority === '' ? null : Number(priority),
      estimate: estimate === '' ? null : Number(estimate),

      /*
        Carried through untouched. See the note above: this input is a
        whole-row replace, so these four have to be re-sent to survive.
        `?? null` / `?? []` cover the create case, where there is no template
        to preserve anything from.
      */
      assigneeId: template?.assigneeId ?? null,
      projectId: template?.projectId ?? null,
      cycleId: template?.cycleId ?? null,
      labelIds: template?.labelIds ?? [],
    })
  }

  const nameError = errorByField.get('name')
  const preservedCount =
    template === null
      ? 0
      : [template.assigneeId, template.projectId, template.cycleId].filter(
          (value) => value !== null,
        ).length + (template.labelIds.length > 0 ? 1 : 0)

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
          Template name
        </label>
        <Input
          aria-describedby={
            nameError === undefined ? `${fieldId}-name-hint` : `${fieldId}-name-error`
          }
          aria-invalid={nameError === undefined ? undefined : true}
          id={`${fieldId}-name`}
          onChange={(event) => {
            setName(event.target.value)
          }}
          required
          value={name}
        />
        {nameError === undefined ? (
          <p className={styles.hint} id={`${fieldId}-name-hint`}>
            What this template is called in the list. Not the issue's title.
          </p>
        ) : (
          <p className={styles.fieldError} id={`${fieldId}-name-error`}>
            {nameError}
          </p>
        )}
      </div>

      <div className={styles.field}>
        <label className={styles.label} htmlFor={`${fieldId}-team`}>
          Team
        </label>
        <Select
          id={`${fieldId}-team`}
          onChange={(event) => {
            setTeamId(event.target.value)
          }}
          value={teamId}
        >
          {/* A template with no team is workspace-wide. Filing from one still
              requires a team -- `IssueCreateFromTemplateInput.teamId` is
              non-null -- so the screen asks then. */}
          <option value="">Any team</option>
          {teams.map((team) => (
            <option key={team.id} value={team.id}>
              {team.key} · {team.name}
            </option>
          ))}
        </Select>
      </div>

      <div className={styles.field}>
        <label className={styles.label} htmlFor={`${fieldId}-title`}>
          Issue title
        </label>
        <Input
          id={`${fieldId}-title`}
          onChange={(event) => {
            setTitle(event.target.value)
          }}
          value={title}
        />
        <p className={styles.hint}>
          What issues made from this template are called. Can be overridden when
          filing.
        </p>
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
          rows={5}
          value={description}
        />
      </div>

      <div className={styles.fieldRow}>
        <div className={styles.field}>
          <label className={styles.label} htmlFor={`${fieldId}-priority`}>
            Priority
          </label>
          <Select
            id={`${fieldId}-priority`}
            onChange={(event) => {
              setPriority(event.target.value)
            }}
            value={priority}
          >
            <option value="">Not set</option>
            {PRIORITY_VALUES.map((value) => (
              <option key={value} value={String(value)}>
                {describePriority(value).name ?? `Priority ${String(value)}`}
              </option>
            ))}
          </Select>
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={`${fieldId}-estimate`}>
            Estimate
          </label>
          <Input
            id={`${fieldId}-estimate`}
            min={0}
            onChange={(event) => {
              setEstimate(event.target.value)
            }}
            type="number"
            value={estimate}
          />
        </div>
      </div>

      {preservedCount > 0 && (
        /*
          Said out loud. This form has no control for an assignee, a project,
          a cycle or labels, and saving re-sends whatever the template already
          had -- so a reader who cannot see those values needs telling that
          they survive, rather than being left to wonder whether a rename just
          dropped them.
        */
        <p className={styles.hint} role="note">
          This template also sets an assignee, project, cycle or labels. Those
          are kept exactly as they are — this form does not edit them.
        </p>
      )}

      <div className={styles.formActions}>
        <Button onClick={onCancel} type="button">
          Cancel
        </Button>
        <Button disabled={isSaving || name.trim() === ''} type="submit" variant="primary">
          {submitLabel}
        </Button>
      </div>
    </form>
  )
}
