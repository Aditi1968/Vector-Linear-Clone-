import { useCallback, useId, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'

import { Button, Input } from '../../../components'
import { useCreateTeam } from '../api'
import { groupFieldErrors } from '../lib/errors'
import type { GroupedErrors } from '../lib/errors'
import {
  isValidTeamKey,
  proposeTeamKey,
  TEAM_KEY_MAX_LENGTH,
  TEAM_KEY_RULE,
} from '../lib/identifiers'
import { onboardingPaths } from '../lib/progress'
import { useOnboardingContext } from './OnboardingLayout'
import styles from '../onboarding.module.css'

/** `NAME_MAX_LENGTH` in `app/services/teams.py`. Mirrored for the hint only. */
const NAME_MAX_LENGTH = 200

const FIELDS = ['name', 'key'] as const

const NO_ERRORS: GroupedErrors = { byField: {}, other: [] }

/**
 * Name the first team and choose its issue key.
 *
 * The key is the part worth care. It is the `ENG` in `ENG-42`, it is stamped
 * into every issue identifier the team ever mints, and those identifiers end
 * up in commit messages, branch names and conversations that outlive any
 * rename. The form says that in as many words rather than leaving someone to
 * discover it from an issue.
 *
 * The server does *not* uppercase the key -- `_validate_key` rejects
 * anything outside `^[A-Z][A-Z0-9]{0,9}$` as it arrives -- so the input
 * uppercases as it is typed and strips the hyphen people reach for out of
 * habit. That is a convenience, not a validator: the rule is still stated
 * beside the field and the server is still the authority.
 */
export function TeamStep() {
  const { membership } = useOnboardingContext()
  const { createTeam, isSubmitting } = useCreateTeam()
  const navigate = useNavigate()

  const [name, setName] = useState('')
  const [keyOverride, setKeyOverride] = useState<string | null>(null)
  const [errors, setErrors] = useState<GroupedErrors>(NO_ERRORS)
  const [formError, setFormError] = useState<string | null>(null)

  const nameRef = useRef<HTMLInputElement>(null)
  const keyRef = useRef<HTMLInputElement>(null)

  const baseId = useId()
  const nameId = `${baseId}-name`
  const nameErrorId = `${baseId}-name-error`
  const keyId = `${baseId}-key`
  const keyHintId = `${baseId}-key-hint`
  const keyErrorId = `${baseId}-key-error`

  // The key tracks the name until it is edited by hand, then stops -- same
  // arrangement as the slug on the previous step, and for the same reason: a
  // field that keeps re-deriving overwrites what someone is typing into it.
  const key = keyOverride ?? proposeTeamKey(name)

  const nameErrors = errors.byField['name'] ?? []
  const keyErrors = errors.byField['key'] ?? []
  const keyLooksWrong = key.length > 0 && !isValidTeamKey(key)

  // What the hint numbers its example issues with: the real key once it is
  // usable, and a placeholder before that, so the sentence reads the same
  // either way rather than collapsing to "-1, -2".
  const example = isValidTeamKey(key) ? key : 'ENG'

  const workspaceSlug = membership?.workspace.slug ?? null

  const submit = useCallback(async () => {
    if (workspaceSlug === null) {
      return
    }

    setErrors(NO_ERRORS)
    setFormError(null)

    const outcome = await createTeam({ workspaceSlug, name, key })

    if (outcome.status === 'ok') {
      /*
       * Advance explicitly, because derived state cannot do it here.
       *
       * Having a team is what "setup is complete" means, so re-deriving
       * would only ever say "done" -- and the two optional steps would be
       * skipped. The guard is written so this is safe rather than a race:
       * `resolveStep` gates the team step on having a *workspace*, not on
       * needing a team, so the refetch landing first does not eject this
       * component before the navigation happens. See ../lib/progress.ts.
       */
      void navigate(onboardingPaths.step('invite'), { replace: true })
      return
    }

    if (outcome.status === 'failed') {
      setFormError(outcome.message)
      return
    }

    const grouped = groupFieldErrors(outcome.errors, FIELDS)
    setErrors(grouped)

    // Focus what was rejected, so the message that just appeared is the one
    // a screen reader announces and a keyboard user is on the control they
    // have to change. `KEY_TAKEN` lands here.
    if ((grouped.byField['name'] ?? []).length > 0) {
      nameRef.current?.focus()
    } else if ((grouped.byField['key'] ?? []).length > 0) {
      keyRef.current?.focus()
    }
  }, [createTeam, key, name, navigate, workspaceSlug])

  const handleSubmit = useCallback(
    (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault()
      void submit()
    },
    [submit],
  )

  return (
    <form className={styles.step} noValidate onSubmit={handleSubmit}>
      <h1 className={styles.title}>Create your first team</h1>
      <p className={styles.lede}>
        Teams own issues, cycles and their own workflow. Most workspaces start
        with one and add more later.
      </p>

      {membership !== null && membership.role === 'MEMBER' && (
        <p className={styles.formError} role="alert">
          Only an admin or owner of {membership.workspace.name} can create a
          team. Ask one of them to set this up.
        </p>
      )}

      {formError !== null && (
        <p className={styles.formError} role="alert">
          {formError}
        </p>
      )}
      {errors.other.length > 0 && (
        <p className={styles.formError} role="alert">
          {errors.other.join(' ')}
        </p>
      )}

      <div className={styles.field}>
        <label className={styles.label} htmlFor={nameId}>
          Team name
        </label>
        <Input
          aria-describedby={nameErrors.length > 0 ? nameErrorId : undefined}
          autoComplete="off"
          autoFocus
          id={nameId}
          invalid={nameErrors.length > 0}
          onChange={(event) => {
            setName(event.target.value)
          }}
          ref={nameRef}
          value={name}
        />
        {name.length > NAME_MAX_LENGTH && (
          <p className={styles.hintWarning}>
            {`Names are limited to ${NAME_MAX_LENGTH} characters.`}
          </p>
        )}
        {nameErrors.length > 0 && (
          <p className={styles.fieldError} id={nameErrorId}>
            {nameErrors.join(' ')}
          </p>
        )}
      </div>

      <div className={styles.field}>
        <label className={styles.label} htmlFor={keyId}>
          Issue key
        </label>
        <Input
          aria-describedby={
            keyErrors.length > 0 ? `${keyHintId} ${keyErrorId}` : keyHintId
          }
          autoCapitalize="characters"
          autoComplete="off"
          autoCorrect="off"
          className={styles.keyInput}
          id={keyId}
          invalid={keyErrors.length > 0 || keyLooksWrong}
          onChange={(event) => {
            // Uppercased and de-hyphenated as typed. Both are what the server
            // requires and neither is a *check* -- an otherwise invalid key
            // still reaches the server and is still refused there.
            setKeyOverride(event.target.value.toUpperCase().replaceAll('-', ''))
          }}
          ref={keyRef}
          spellCheck={false}
          value={key}
        />
        <p
          className={keyLooksWrong ? styles.hintWarning : styles.hint}
          id={keyHintId}
        >
          {`Every issue this team creates is numbered from this key -- ${example}-1, ${example}-2, and so on. Those identifiers outlive renames and end up in branch names and commit messages, so pick something short you will still recognise in a year. ${TEAM_KEY_RULE}`}
          {key.length > TEAM_KEY_MAX_LENGTH &&
            ` This one is ${key.length} characters.`}
        </p>
        {keyErrors.length > 0 && (
          <p className={styles.fieldError} id={keyErrorId}>
            {keyErrors.join(' ')}
          </p>
        )}
      </div>

      <div className={styles.actions}>
        <Button loading={isSubmitting} type="submit" variant="primary">
          {isSubmitting ? 'Creating team...' : 'Create team'}
        </Button>
      </div>
    </form>
  )
}
