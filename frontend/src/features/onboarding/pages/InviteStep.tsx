import { useCallback, useId, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { Link } from 'react-router-dom'

import { Button, Input, Select } from '../../../components'
import { useCreateInvitation } from '../api'
import type { CreatedInvitation } from '../api'
import { groupFieldErrors } from '../lib/errors'
import type { GroupedErrors } from '../lib/errors'
import { onboardingPaths } from '../lib/progress'
import { useOnboardingContext } from './OnboardingLayout'
import styles from '../onboarding.module.css'

const FIELDS = ['email', 'role'] as const

const NO_ERRORS: GroupedErrors = { byField: {}, other: [] }

/**
 * The roles `invitationCreate` accepts, with what each one means.
 *
 * `WorkspaceRole` from the generated schema is the source of the values; the
 * sentences are this app's explanation of them. OWNER is offered because the
 * enum has it and an owner may hand out ownership, but it is not the default
 * and the description says why not.
 */
const ROLES = [
  { value: 'MEMBER', label: 'Member -- can work on issues' },
  { value: 'ADMIN', label: 'Admin -- can also manage teams and people' },
  { value: 'OWNER', label: 'Owner -- full control, including billing later' },
] as const

/** One invitation that has been created, plus the link built from its token. */
interface IssuedInvite extends CreatedInvitation {
  link: string
}

/**
 * Invite teammates -- and be honest that Vector will not send the email.
 *
 * There is no mail delivery in this deployment. `invitationCreate` mints a
 * token, returns it **once**, and no query can read it back; if this screen
 * does not put it in front of the person who asked for it, it is gone and
 * the invitation is unusable. So each created invitation stays on screen
 * with its link and a copy control, the page says in plain words that
 * nothing was sent, and the step warns before it lets someone leave with
 * links they have not copied.
 *
 * That warning is the one piece of interaction here that is not obvious, and
 * it is not paternalism: leaving this page is genuinely destructive, in a way
 * that looks exactly like leaving any other page.
 *
 * Skippable, because it is: a workspace with no invitations is a finished
 * workspace, which is also why nothing about this step is part of the state
 * machine in ../lib/progress.ts.
 */
export function InviteStep() {
  const { membership } = useOnboardingContext()
  const { createInvitation, isSubmitting } = useCreateInvitation()

  const [copyStatus, setCopyStatus] = useState('')
  const [email, setEmail] = useState('')
  const [role, setRole] = useState<(typeof ROLES)[number]['value']>('MEMBER')
  const [issued, setIssued] = useState<IssuedInvite[]>([])
  const [errors, setErrors] = useState<GroupedErrors>(NO_ERRORS)
  const [formError, setFormError] = useState<string | null>(null)

  const emailRef = useRef<HTMLInputElement>(null)

  const baseId = useId()
  const emailId = `${baseId}-email`
  const emailErrorId = `${baseId}-email-error`
  const roleId = `${baseId}-role`

  const emailErrors = errors.byField['email'] ?? []
  const workspaceSlug = membership?.workspace.slug ?? null

  const submit = useCallback(async () => {
    if (workspaceSlug === null) {
      return
    }

    setErrors(NO_ERRORS)
    setFormError(null)

    const outcome = await createInvitation({ workspaceSlug, email, role })

    if (outcome.status === 'failed') {
      setFormError(outcome.message)
      return
    }

    if (outcome.status === 'rejected') {
      setErrors(groupFieldErrors(outcome.errors, FIELDS))
      emailRef.current?.focus()
      return
    }

    setIssued((current) => [
      ...current,
      {
        ...outcome.value,
        // Absolute, because this is going into a chat message or an email
        // somebody else will open. `window.location.origin` and not a
        // configured base URL: the link has to work from wherever this app is
        // actually being served, and that is the only thing that knows.
        link: `${window.location.origin}${onboardingPaths.acceptInvite(outcome.value.token)}`,
      },
    ])

    setEmail('')
    // Back to the field, so inviting a second person is typing rather than
    // hunting for where focus went.
    emailRef.current?.focus()
  }, [createInvitation, email, role, workspaceSlug])

  const handleSubmit = useCallback(
    (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault()
      void submit()
    },
    [submit],
  )

  /*
   * Copy one link, and say whether it worked.
   *
   * `navigator.clipboard` is not always there -- an insecure origin, an old
   * browser, a permission refused -- and the failure is silent. Unreported,
   * it leaves someone believing they hold a link that is not on their
   * clipboard, and this is the one string in the product that cannot be
   * fetched again. So both outcomes are announced, and the link is
   * selectable text on screen either way.
   *
   * A live region rather than a toast: `<ToastProvider>` is not mounted in
   * this application yet and `useToast()` throws without it. Three lines of
   * `role="status"` do the announcing part, which is the part that matters.
   */
  const copy = useCallback((invite: IssuedInvite) => {
    void navigator.clipboard
      ?.writeText(invite.link)
      .then(() => {
        setCopyStatus(`Invite link for ${invite.invitation.email} copied.`)
      })
      .catch(() => {
        setCopyStatus('Could not copy. Select the link and copy it manually.')
      })
  }, [])

  const uncopiedWarning =
    issued.length > 0
      ? `You have ${issued.length} invite ${issued.length === 1 ? 'link' : 'links'} on this page. They are shown once -- copy them before you continue.`
      : null

  return (
    <div className={styles.step}>
      <h1 className={styles.title}>Invite your teammates</h1>
      <p className={styles.lede}>
        Optional. You can do this any time from workspace settings.
      </p>

      {/*
       * Stated before anyone submits, not after. Discovering that no email
       * was sent *after* closing the page is discovering it too late.
       */}
      <p className={styles.notice}>
        <strong>Vector cannot send email yet.</strong> Creating an invitation
        gives you a link, shown here once and never again. You send it
        yourself.
      </p>

      <form className={styles.inviteForm} noValidate onSubmit={handleSubmit}>
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
          <label className={styles.label} htmlFor={emailId}>
            Email address
          </label>
          <Input
            aria-describedby={emailErrors.length > 0 ? emailErrorId : undefined}
            autoComplete="off"
            id={emailId}
            invalid={emailErrors.length > 0}
            onChange={(event) => {
              setEmail(event.target.value)
            }}
            ref={emailRef}
            // `type="email"` for the keyboard it brings up on a phone. The
            // browser's own validity check is off (`noValidate` on the form),
            // so the server's rule stays the one that decides.
            type="email"
            value={email}
          />
          {emailErrors.length > 0 && (
            <p className={styles.fieldError} id={emailErrorId}>
              {emailErrors.join(' ')}
            </p>
          )}
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={roleId}>
            Role
          </label>
          <Select
            id={roleId}
            onChange={(event) => {
              // Narrowed against the same list the options come from, so a
              // value that is not a role cannot reach the mutation.
              const chosen = ROLES.find(
                (option) => option.value === event.target.value,
              )

              if (chosen !== undefined) {
                setRole(chosen.value)
              }
            }}
            value={role}
          >
            {ROLES.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </Select>
        </div>

        <Button loading={isSubmitting} type="submit">
          {isSubmitting ? 'Creating invite...' : 'Create invite link'}
        </Button>
      </form>

      {issued.length > 0 && (
        <section aria-labelledby={`${baseId}-issued`} className={styles.issued}>
          <h2 className={styles.subtitle} id={`${baseId}-issued`}>
            Invite links
          </h2>
          <ul className={styles.inviteList}>
            {issued.map((invite) => (
              <li className={styles.inviteRow} key={invite.invitation.id}>
                <div className={styles.inviteMeta}>
                  <span className={styles.inviteEmail}>
                    {invite.invitation.email}
                  </span>
                  <span className={styles.inviteRole}>
                    {invite.invitation.role.toLowerCase()}
                  </span>
                </div>
                {/*
                 * The link as selectable text, not only behind a button.
                 * `navigator.clipboard` is unavailable on an insecure origin
                 * and can be refused anywhere, and this is the one string in
                 * the product that cannot be fetched again.
                 */}
                <code className={styles.inviteLink}>{invite.link}</code>
                <Button
                  aria-label={`Copy invite link for ${invite.invitation.email}`}
                  onClick={() => {
                    copy(invite)
                  }}
                  size="sm"
                >
                  Copy
                </Button>
              </li>
            ))}
          </ul>
        </section>
      )}

      {uncopiedWarning !== null && (
        <p className={styles.notice}>{uncopiedWarning}</p>
      )}

      {/*
       * Always in the DOM, empty until there is something to say. A live
       * region added to the page at the moment it gains text is often not
       * announced at all -- the assistive technology has to be observing it
       * before the change happens.
       */}
      <p className={styles.copyStatus} role="status">
        {copyStatus}
      </p>

      <div className={styles.actions}>
        <Link
          className={styles.advance}
          to={onboardingPaths.step('integrations')}
        >
          {issued.length > 0 ? 'Continue' : 'Skip for now'}
        </Link>
      </div>
    </div>
  )
}
