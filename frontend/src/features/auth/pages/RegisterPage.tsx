import { useState } from 'react'
import { Link } from 'react-router-dom'
import type { FormEvent } from 'react'

import { Button } from '../../../components'
import { publicPaths } from '../../../app/routes/paths'
import { useRegister } from '../api'
import { AuthField, AuthScreen, FormAlert } from '../components/AuthScreen'
import { textField, useAuthFormErrors } from '../lib/formErrors'
import styles from '../auth.module.css'

/** See LoginPage: module scope, because the errors hook memoises on identity. */
const FIELDS = ['name', 'email', 'password'] as const

/**
 * `app.services.auth.PASSWORD_MIN_LENGTH`, restated for `minLength`.
 *
 * The browser's own check, so the obvious mistake costs no round trip. It is
 * a convenience and not the rule: the server validates the same thing and its
 * message is what gets rendered, which is what keeps this constant from
 * quietly becoming a second, weaker policy if the backend raises the floor.
 */
const PASSWORD_MIN_LENGTH = 8

/**
 * Create an account.
 *
 * Registering signs you in, so there is no "now go and log in" step: the
 * mutation writes the viewer into the cache and `RequireNoAuth` redirects --
 * to onboarding, since a brand new account has no workspace.
 *
 * `name` is optional in the schema and optional here. An empty box is sent as
 * null rather than as `""`, because the column is nullable and "" would be a
 * name nobody chose that every avatar and mention would then have to special-
 * case.
 */
export function RegisterPage() {
  const { submit, isSubmitting } = useRegister()
  const { errors, report, formRef, alertRef } = useAuthFormErrors(FIELDS)
  const [handedOff, setHandedOff] = useState(false)

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault()

    const fields = new FormData(event.currentTarget)
    const name = textField(fields, 'name').trim()

    const outcome = await submit({
      email: textField(fields, 'email'),
      password: textField(fields, 'password'),
      name: name === '' ? null : name,
    })

    report(outcome)
    setHandedOff(outcome.status === 'signedIn')
  }

  return (
    <AuthScreen
      title="Create your Vector account"
      lead="Start tracking work in minutes. No credit card, no trial clock."
      footer={
        <>
          Already have an account? <Link to={publicPaths.login()}>Sign in</Link>
        </>
      }
    >
      <form
        ref={formRef}
        className={styles.form}
        onSubmit={(event) => {
          void handleSubmit(event)
        }}
      >
        <FormAlert message={errors.form} alertRef={alertRef} />

        <AuthField
          name="name"
          label="Name"
          hint="Optional. What your teammates will see."
          type="text"
          autoComplete="name"
          autoFocus
          error={errors.byField.name}
        />

        <AuthField
          name="email"
          label="Email"
          type="email"
          autoComplete="username"
          required
          error={errors.byField.email}
        />

        <AuthField
          name="password"
          label="Password"
          hint={`At least ${String(PASSWORD_MIN_LENGTH)} characters.`}
          type="password"
          autoComplete="new-password"
          required
          minLength={PASSWORD_MIN_LENGTH}
          error={errors.byField.password}
        />

        <Button type="submit" variant="primary" fullWidth loading={isSubmitting || handedOff}>
          Create account
        </Button>
      </form>
    </AuthScreen>
  )
}
