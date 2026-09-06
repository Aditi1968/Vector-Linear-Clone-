import { useState } from 'react'
import { Link } from 'react-router-dom'
import type { FormEvent } from 'react'

import { Button } from '../../../components'
import { useAppPaths } from '../../../app/routes/useAppPaths'
import { useLogin } from '../api'
import { AuthField, AuthScreen, FormAlert } from '../components/AuthScreen'
import { textField, useAuthFormErrors } from '../lib/formErrors'
import styles from '../auth.module.css'

/**
 * The inputs the server may attribute an error to.
 *
 * Module scope because `useAuthFormErrors` memoises on this array's identity;
 * declared inline it would be a new array every render.
 *
 * `credentials` is deliberately absent. It is the field the backend reports a
 * failed log-in under, and it names no input on purpose -- so it falls
 * through to the form-level alert, which is exactly where a message that
 * refuses to say which half was wrong belongs.
 */
const FIELDS = ['email', 'password'] as const

/**
 * Sign in.
 *
 * A real `<form>` with a real submit button, which is what makes Enter work
 * from either field and what makes a password manager recognise the page at
 * all. The inputs are uncontrolled and read with `FormData` on submit:
 * autofill writes straight to the DOM, and a controlled input whose state
 * never saw that write is the classic way a manager-filled form submits
 * empty.
 *
 * Nothing is stored. There is no token to keep -- the server answers with an
 * HttpOnly cookie -- so this page holds no credential after the request, and
 * `localStorage` is not touched here or anywhere else in the feature.
 *
 * It does not navigate on success either. Writing the viewer into the cache
 * is enough: `RequireNoAuth`, which wraps this route, sees somebody signed in
 * and performs the redirect. One place decides where sign-in lands.
 */
export function LoginPage() {
  const paths = useAppPaths()
  const { submit, isSubmitting } = useLogin()
  const { errors, report, formRef, alertRef } = useAuthFormErrors(FIELDS)
  const [handedOff, setHandedOff] = useState(false)

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault()

    // Read before the await: the form element is reachable through
    // `currentTarget` only for the synchronous part of the handler.
    const fields = new FormData(event.currentTarget)

    const outcome = await submit({
      email: textField(fields, 'email'),
      password: textField(fields, 'password'),
    })

    report(outcome)

    // Keeps the button busy through the guard's redirect. Without it the
    // form goes idle for a frame and looks like it did nothing.
    setHandedOff(outcome.status === 'signedIn')
  }

  return (
    <AuthScreen
      title="Sign in to Vector"
      lead="Pick up where your team left off."
      footer={
        <>
          New to Vector? <Link to={paths.register()}>Create an account</Link>
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
          name="email"
          label="Email"
          type="email"
          // `username` rather than `email`: this is the account identifier,
          // and it is the value password managers pair a stored password
          // with. `type="email"` still gets the right keyboard and the
          // browser's own address check.
          autoComplete="username"
          required
          autoFocus
          error={errors.byField.email}
        />

        <AuthField
          name="password"
          label="Password"
          type="password"
          autoComplete="current-password"
          required
          error={errors.byField.password}
        />

        <Button type="submit" variant="primary" fullWidth loading={isSubmitting || handedOff}>
          Sign in
        </Button>
      </form>
    </AuthScreen>
  )
}
