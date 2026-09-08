import { useId, useMemo, useState } from 'react'
import type { FormEvent } from 'react'

import { Button, Input, Select } from '../../../components'
import type { Environment } from '../../environments/api'
import type { GithubIntegration } from '../../settings/api'
import type { ReleaseDraft, ReleaseValidationError } from '../api'
import styles from '../releases.module.css'

export interface ReleaseFormProps {
  environments: readonly Environment[]
  repositories: GithubIntegration['repositories']
  isSaving: boolean
  errors: readonly ReleaseValidationError[]
  errorMessage: string | null
  onCancel: () => void
  onSubmit: (draft: ReleaseDraft) => void
}

/**
 * The 40-character lowercase hex the database will accept, and nothing else.
 *
 * `releases_commit_sha_format` in migration 024 restates
 * `github_commits_sha_format` for a reason worth repeating at the input: this
 * SHA is looked up by equality to resolve the release's range, so an
 * abbreviation, a branch name or a URL would be stored happily by a looser
 * column and then silently match nothing. The pattern is on the field as a
 * native `pattern` attribute so the browser refuses before a round trip; the
 * database is still the authority.
 */
const SHA_PATTERN = '[0-9a-f]{40}'

/**
 * Cut a release: what shipped, where it is going, and from which commit.
 *
 * ## Both pickers can be empty, and each means something different
 *
 * A release names an environment and a repository, and neither is free text --
 * `releases_environment_fk` and `releases_repository_fk` are composite through
 * `workspace_id`, so an id from anywhere else has no row to match. So a
 * workspace with no environment declared, or with GitHub not connected, simply
 * cannot cut a release, and this form says which of the two is missing and
 * where to go rather than presenting an empty menu.
 *
 * ## The previous commit is normally left blank
 *
 * It is the lower bound of the range the notes are generated from, and the
 * server resolves it itself from the last deploy of this repository into this
 * environment -- migration 024 indexes exactly that lookup. Supplying one here
 * overrides that, which is right only when a person genuinely knows better
 * than the last recorded deploy: a first release into a new environment does
 * not need it (the server takes "everything up to this commit"), and a routine
 * deploy should not want it.
 */
export function ReleaseForm({
  environments,
  repositories,
  isSaving,
  errors,
  errorMessage,
  onCancel,
  onSubmit,
}: ReleaseFormProps) {
  const fieldId = useId()

  const [name, setName] = useState('')
  const [environmentId, setEnvironmentId] = useState('')
  const [repositoryId, setRepositoryId] = useState('')
  const [commitSha, setCommitSha] = useState('')
  const [previousCommitSha, setPreviousCommitSha] = useState('')

  const errorByField = useMemo(() => {
    const map = new Map<string, string>()

    for (const entry of errors) {
      if (!map.has(entry.field)) {
        map.set(entry.field, entry.message)
      }
    }

    return map
  }, [errors])

  const canSubmit =
    name.trim() !== '' &&
    environmentId !== '' &&
    repositoryId !== '' &&
    commitSha.trim() !== ''

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()

    const previous = previousCommitSha.trim()

    onSubmit({
      name: name.trim(),
      environmentId,
      repositoryId,
      commitSha: commitSha.trim(),
      // Blank travels as null, never as `''`: an empty string is a value the
      // server would check against the SHA format and refuse, where null is
      // the documented "resolve the previous deploy yourself".
      previousCommitSha: previous === '' ? null : previous,
    })
  }

  if (environments.length === 0 || repositories.length === 0) {
    return (
      <div className={styles.formBlocked}>
        <p>
          {environments.length === 0
            ? 'This workspace has no environment to deploy to. Declare one on the Environments screen first — a release has to name a target, and the database will not accept one without.'
            : 'This workspace has no GitHub repository connected. A release names the repository it shipped, and only repositories the workspace’s installation covers can be named. Connect GitHub in Settings first.'}
        </p>
        <div className={styles.formActions}>
          <Button onClick={onCancel} type="button" variant="secondary">
            Close
          </Button>
        </div>
      </div>
    )
  }

  const nameError = errorByField.get('name')
  const commitError = errorByField.get('commitSha')
  const previousError = errorByField.get('previousCommitSha')
  const environmentError = errorByField.get('environmentId')
  const repositoryError = errorByField.get('repositoryId')

  return (
    <form className={styles.form} noValidate onSubmit={handleSubmit}>
      {errorMessage !== null && (
        <p className={styles.formError} role="alert">
          {errorMessage}
        </p>
      )}

      <div className={styles.field}>
        {/* "Version" and not "Name": the release list beside this dialog is a
            list of versions, and the word is what a person actually types --
            "v1.4.0", "2026-09-07.2", "hotfix-oauth". */}
        <label className={styles.label} htmlFor={`${fieldId}-name`}>
          Version
        </label>
        <Input
          aria-describedby={nameError === undefined ? undefined : `${fieldId}-name-error`}
          aria-invalid={nameError === undefined ? undefined : true}
          id={`${fieldId}-name`}
          maxLength={200}
          onChange={(event) => {
            setName(event.target.value)
          }}
          placeholder="v1.4.0"
          required
          value={name}
        />
        {nameError !== undefined && (
          <p className={styles.fieldError} id={`${fieldId}-name-error`}>
            {nameError}
          </p>
        )}
        <p className={styles.hint}>
          Not unique, deliberately. The same version ships to staging and then to
          production, and again after a rollback.
        </p>
      </div>

      <div className={styles.field}>
        <label className={styles.label} htmlFor={`${fieldId}-environment`}>
          Environment
        </label>
        <Select
          aria-invalid={environmentError === undefined ? undefined : true}
          id={`${fieldId}-environment`}
          onChange={(event) => {
            setEnvironmentId(event.target.value)
          }}
          required
          value={environmentId}
        >
          <option value="">Choose a target…</option>
          {environments.map((environment) => (
            <option key={environment.id} value={environment.id}>
              {environment.name}
            </option>
          ))}
        </Select>
        {environmentError !== undefined && (
          <p className={styles.fieldError}>{environmentError}</p>
        )}
      </div>

      <div className={styles.field}>
        <label className={styles.label} htmlFor={`${fieldId}-repository`}>
          Repository
        </label>
        <Select
          aria-invalid={repositoryError === undefined ? undefined : true}
          id={`${fieldId}-repository`}
          onChange={(event) => {
            setRepositoryId(event.target.value)
          }}
          required
          value={repositoryId}
        >
          <option value="">Choose a repository…</option>
          {repositories.map((repository) => (
            <option key={repository.repositoryId} value={repository.repositoryId}>
              {repository.fullName}
            </option>
          ))}
        </Select>
        {repositoryError !== undefined && (
          <p className={styles.fieldError}>{repositoryError}</p>
        )}
      </div>

      <div className={styles.field}>
        <label className={styles.label} htmlFor={`${fieldId}-commit`}>
          Commit
        </label>
        <Input
          aria-describedby={`${fieldId}-commit-hint`}
          aria-invalid={commitError === undefined ? undefined : true}
          className={styles.mono}
          id={`${fieldId}-commit`}
          maxLength={40}
          onChange={(event) => {
            setCommitSha(event.target.value)
          }}
          pattern={SHA_PATTERN}
          required
          value={commitSha}
        />
        {commitError !== undefined && (
          <p className={styles.fieldError}>{commitError}</p>
        )}
        <p className={styles.hint} id={`${fieldId}-commit-hint`}>
          The full 40-character SHA. An abbreviation is ambiguous by construction and
          is refused.
        </p>
      </div>

      <div className={styles.field}>
        <label className={styles.label} htmlFor={`${fieldId}-previous`}>
          Previous commit
        </label>
        <Input
          aria-describedby={`${fieldId}-previous-hint`}
          aria-invalid={previousError === undefined ? undefined : true}
          className={styles.mono}
          id={`${fieldId}-previous`}
          maxLength={40}
          onChange={(event) => {
            setPreviousCommitSha(event.target.value)
          }}
          pattern={SHA_PATTERN}
          value={previousCommitSha}
        />
        {previousError !== undefined && (
          <p className={styles.fieldError}>{previousError}</p>
        )}
        <p className={styles.hint} id={`${fieldId}-previous-hint`}>
          Optional. Left blank, the server starts the range at whatever was last
          deployed to this environment from this repository.
        </p>
      </div>

      <div className={styles.formActions}>
        <Button onClick={onCancel} type="button" variant="ghost">
          Cancel
        </Button>
        <Button disabled={isSaving || !canSubmit} type="submit" variant="primary">
          Cut release
        </Button>
      </div>
    </form>
  )
}
