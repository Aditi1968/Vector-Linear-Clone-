import { useId, useMemo, useState } from 'react'
import type { FormEvent } from 'react'

import { Button, Input, Select } from '../../../components'
import type { WorkspaceProject } from '../../issues/api'
import type { Initiative } from '../../initiatives/api'
import type { DocumentDraft, DocumentValidationError } from '../api'
import styles from '../documents.module.css'

export interface DocumentFormProps {
  projects: readonly WorkspaceProject[]
  initiatives: readonly Initiative[]
  isSaving: boolean
  errors: readonly DocumentValidationError[]
  errorMessage: string | null
  onCancel: () => void
  onSubmit: (draft: DocumentDraft) => void
}

/**
 * The new-document form: a title, and optionally what it belongs to.
 *
 * ## One picker for two fields
 *
 * `DocumentCreateInput` has `projectId` and `initiativeId` as separate
 * nullable fields, and migration 023's `documents_one_parent` check
 * (`num_nonnulls(project_id, initiative_id) <= 1`) refuses a row that sets
 * both. Two pickers would let somebody choose both
 * and meet that refusal after submitting; one picker whose options are drawn
 * from both lists cannot express the invalid state at all. The value carries
 * its own kind (`project:<id>` / `initiative:<id>`) so the submit knows which
 * field to fill.
 *
 * The lists behind it are one page each -- the workspace context's first 50
 * projects and the initiative list's first 25 -- so a document can be
 * attached here only to something on those pages. Said on screen rather than
 * left to be discovered by a project that is simply not in the menu.
 */
export function DocumentForm({
  projects,
  initiatives,
  isSaving,
  errors,
  errorMessage,
  onCancel,
  onSubmit,
}: DocumentFormProps) {
  const fieldId = useId()

  const [title, setTitle] = useState('')
  const [parent, setParent] = useState('')

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

    const [kind, id] = parent.split(':')

    onSubmit({
      title: title.trim(),
      projectId: kind === 'project' ? (id ?? null) : null,
      initiativeId: kind === 'initiative' ? (id ?? null) : null,
    })
  }

  const titleError = errorByField.get('title')

  return (
    <form className={styles.form} noValidate onSubmit={handleSubmit}>
      {errorMessage !== null && (
        <p className={styles.formError} role="alert">
          {errorMessage}
        </p>
      )}

      <div className={styles.field}>
        {/*
          "Title" and not "Document title", which is what the open document's
          rename box is called. Both would be on the page at once with the
          composer open, and two controls with one name is a screen you cannot
          navigate by voice or by name lookup -- the label is the only thing
          telling them apart. The dialog's own heading supplies the rest of
          the context here.
        */}
        <label className={styles.label} htmlFor={`${fieldId}-title`}>
          Title
        </label>
        <Input
          aria-describedby={titleError === undefined ? undefined : `${fieldId}-title-error`}
          aria-invalid={titleError === undefined ? undefined : true}
          id={`${fieldId}-title`}
          onChange={(event) => {
            setTitle(event.target.value)
          }}
          required
          value={title}
        />
        {titleError !== undefined && (
          <p className={styles.fieldError} id={`${fieldId}-title-error`}>
            {titleError}
          </p>
        )}
      </div>

      <div className={styles.field}>
        <label className={styles.label} htmlFor={`${fieldId}-parent`}>
          Belongs to
        </label>
        <Select
          id={`${fieldId}-parent`}
          onChange={(event) => {
            setParent(event.target.value)
          }}
          value={parent}
        >
          {/* A document attached to neither is a workspace document, which is
              the commonest kind and so the default. */}
          <option value="">The workspace</option>
          {projects.length > 0 && (
            <optgroup label="Projects">
              {projects.map((project) => (
                <option key={project.id} value={`project:${project.id}`}>
                  {project.name}
                </option>
              ))}
            </optgroup>
          )}
          {initiatives.length > 0 && (
            <optgroup label="Initiatives">
              {initiatives.map((initiative) => (
                <option key={initiative.id} value={`initiative:${initiative.id}`}>
                  {initiative.name}
                </option>
              ))}
            </optgroup>
          )}
        </Select>
        <p className={styles.hint}>
          The menu lists the projects and initiatives already loaded on this screen. A
          workspace with more than a page of either will not show all of them here.
        </p>
      </div>

      <div className={styles.formActions}>
        <Button onClick={onCancel} type="button" variant="ghost">
          Cancel
        </Button>
        <Button disabled={isSaving || title.trim() === ''} type="submit" variant="primary">
          Create document
        </Button>
      </div>
    </form>
  )
}
