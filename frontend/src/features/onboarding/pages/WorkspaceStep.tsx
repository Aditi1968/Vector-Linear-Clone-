import { useCallback, useId, useRef, useState } from 'react'
import type { FormEvent } from 'react'

import { Button, Input } from '../../../components'
import { useCreateWorkspace } from '../api'
import { groupFieldErrors } from '../lib/errors'
import type { GroupedErrors } from '../lib/errors'
import { isValidSlug, SLUG_RULE, slugify } from '../lib/identifiers'
import styles from '../onboarding.module.css'

/**
 * `SLUG_MAX_LENGTH` in `app/services/memberships.py` -- one DNS label.
 *
 * Mirrored for the counter and the hint only. The input deliberately has no
 * `maxLength`: a native limit makes the server's `TOO_LONG` rule unreachable,
 * so the form would be enforcing a rule it merely believes in and would keep
 * enforcing the old one the day the server changed.
 */
const SLUG_MAX_LENGTH = 63

/** `NAME_MAX_LENGTH`, same file. Same reasoning about not being enforced. */
const NAME_MAX_LENGTH = 200

const FIELDS = ['name', 'slug'] as const

const NO_ERRORS: GroupedErrors = { byField: {}, other: [] }

/**
 * Name the workspace and choose its URL.
 *
 * The slug tracks the name until it is edited by hand, and then stops. That
 * is the whole of the interaction and it is worth stating why it is not
 * simpler: a slug that keeps re-deriving would overwrite what someone typed
 * while they were typing it, and a slug that never derives makes every new
 * workspace a two-field form for a one-field decision.
 *
 * There is no success message and no explicit navigation. Creating the
 * workspace refetches `myWorkspaces`, the guard in ./OnboardingLayout
 * re-derives progress from it, and the team step is what renders next --
 * which means the redirect that happens after a submit is the same code path
 * as the redirect that happens after a refresh. One path, tested once.
 */
export function WorkspaceStep() {
  const { createWorkspace, isSubmitting } = useCreateWorkspace()

  const [name, setName] = useState('')
  const [slugOverride, setSlugOverride] = useState<string | null>(null)
  const [errors, setErrors] = useState<GroupedErrors>(NO_ERRORS)
  const [formError, setFormError] = useState<string | null>(null)

  const nameRef = useRef<HTMLInputElement>(null)
  const slugRef = useRef<HTMLInputElement>(null)

  // `useId` rather than fixed ids: two of these on one page -- or one
  // rendered twice in a test -- would otherwise produce duplicate ids and
  // every `aria-describedby` would point at the wrong element.
  const baseId = useId()
  const nameId = `${baseId}-name`
  const nameErrorId = `${baseId}-name-error`
  const slugId = `${baseId}-slug`
  const slugHintId = `${baseId}-slug-hint`
  const slugErrorId = `${baseId}-slug-error`

  const slug = slugOverride ?? slugify(name)

  const nameErrors = errors.byField['name'] ?? []
  const slugErrors = errors.byField['slug'] ?? []

  // Only once there is something to judge. Reddening an empty field before
  // anyone has typed in it is noise, not feedback.
  const slugLooksWrong =
    slug.length > 0 && (!isValidSlug(slug) || slug.length > SLUG_MAX_LENGTH)

  const submit = useCallback(async () => {
    setErrors(NO_ERRORS)
    setFormError(null)

    const outcome = await createWorkspace({
      // Sent as typed. The server strips the name itself and validates the
      // slug raw -- it does not lowercase or trim it -- so trimming here
      // would mean the client and the server disagree about what was
      // submitted.
      name,
      slug,
    })

    if (outcome.status === 'ok') {
      // Nothing to do. `myWorkspaces` has been refetched, so the guard is
      // already re-deriving and this component is about to unmount.
      return
    }

    if (outcome.status === 'failed') {
      setFormError(outcome.message)
      return
    }

    const grouped = groupFieldErrors(outcome.errors, FIELDS)
    setErrors(grouped)

    /*
     * Focus the first field the server rejected.
     *
     * This is what makes the slug-taken case usable rather than merely
     * reported: `SLUG_TAKEN` arrives on the `slug` field, the message is
     * rendered into the element `aria-describedby` already points at, the
     * input is marked `aria-invalid`, and focus lands on it -- so a screen
     * reader announces the field, its label and the reason together, and a
     * keyboard user is on the control they have to change. Without this,
     * focus stays on the submit button and the message that just appeared
     * above it is never announced at all.
     */
    if ((grouped.byField['name'] ?? []).length > 0) {
      nameRef.current?.focus()
    } else if ((grouped.byField['slug'] ?? []).length > 0) {
      slugRef.current?.focus()
    }
  }, [createWorkspace, name, slug])

  const handleSubmit = useCallback(
    (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault()
      // The handler stays synchronous: an async function passed straight to
      // `onSubmit` returns a promise React will not await, and a rejection
      // inside it becomes an unhandled rejection.
      void submit()
    },
    [submit],
  )

  return (
    <form className={styles.step} noValidate onSubmit={handleSubmit}>
      <h1 className={styles.title}>Create your workspace</h1>
      <p className={styles.lede}>
        A workspace holds your teams, issues and projects. You will be its
        owner.
      </p>

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
          Workspace name
        </label>
        <Input
          aria-describedby={nameErrors.length > 0 ? nameErrorId : undefined}
          autoComplete="organization"
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
        <label className={styles.label} htmlFor={slugId}>
          Workspace URL
        </label>
        <Input
          // Always described by the rule, and additionally by the error when
          // there is one. The rule is not conditional: it is what the field
          // is *for*, and a hint that appears only once something is wrong
          // is a hint nobody hears in time.
          aria-describedby={
            slugErrors.length > 0 ? `${slugHintId} ${slugErrorId}` : slugHintId
          }
          autoCapitalize="none"
          autoComplete="off"
          autoCorrect="off"
          id={slugId}
          invalid={slugErrors.length > 0 || slugLooksWrong}
          onChange={(event) => {
            // From here on the slug is the person's, not the name's.
            setSlugOverride(event.target.value)
          }}
          ref={slugRef}
          spellCheck={false}
          value={slug}
        />
        <p
          className={slugLooksWrong ? styles.hintWarning : styles.hint}
          id={slugHintId}
        >
          {SLUG_RULE}
          {slug.length > SLUG_MAX_LENGTH &&
            ` At most ${SLUG_MAX_LENGTH} characters; this one is ${slug.length}.`}
        </p>
        {slugErrors.length > 0 && (
          <p className={styles.fieldError} id={slugErrorId}>
            {slugErrors.join(' ')}
          </p>
        )}
      </div>

      <div className={styles.actions}>
        <Button loading={isSubmitting} type="submit" variant="primary">
          {isSubmitting ? 'Creating workspace...' : 'Create workspace'}
        </Button>
      </div>
    </form>
  )
}
