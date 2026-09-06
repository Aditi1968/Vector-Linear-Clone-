import { Link } from 'react-router-dom'
import type { ReactNode, RefObject } from 'react'

import { Input, VectorMark } from '../../../components'
import type { InputProps } from '../../../components'
import { useAppPaths } from '../../../app/routes/useAppPaths'
import { errorId } from '../lib/formErrors'
import styles from '../auth.module.css'

export interface AuthScreenProps {
  /** The `<h1>`. There is exactly one, and it is this. */
  title: string
  /** One sentence under the title saying what the form is for. */
  lead: string
  /** The form. */
  children: ReactNode
  /** The other door -- "Create an account", "Sign in". */
  footer: ReactNode
}

/**
 * The frame both auth pages sit in.
 *
 * Its own `<main>` and its own `<h1>`, because these pages render outside the
 * application shell: a signed-out visitor has no sidebar, no workspace and no
 * page header, and mounting the shell around a sign-in form would put
 * navigation to places they cannot go beside the form that would let them.
 */
export function AuthScreen({ title, lead, children, footer }: AuthScreenProps) {
  const paths = useAppPaths()

  return (
    <div className={styles.screen}>
      <main className={styles.card}>
        <Link to={paths.landing()} className={styles.brand}>
          <VectorMark className={styles.brandMark} />
          <span>Vector</span>
        </Link>

        <h1 className={styles.title}>{title}</h1>
        <p className={styles.lead}>{lead}</p>

        {children}
      </main>

      <p className={styles.footer}>{footer}</p>
    </div>
  )
}

export interface FormAlertProps {
  message: string | null
  /** Focused by `useAuthFormErrors` when no single input is at fault. */
  alertRef: RefObject<HTMLDivElement | null>
}

/**
 * The errors that name no input.
 *
 * `role="alert"` so it is announced when it appears, and `tabIndex={-1}` so
 * focus can be moved to it -- a failed sign-in whose cause the server will not
 * attribute to a field has nowhere else for the keyboard to go.
 *
 * Rendered only when there is something to say. An always-present empty live
 * region would be the safer pattern for text that *changes*, but this one
 * appears and disappears with the whole message, which browsers announce
 * reliably.
 */
export function FormAlert({ message, alertRef }: FormAlertProps) {
  if (message === null) {
    return null
  }

  return (
    <div ref={alertRef} className={styles.formAlert} role="alert" tabIndex={-1}>
      {message}
    </div>
  )
}

export interface AuthFieldProps extends Omit<InputProps, 'id' | 'invalid'> {
  /** Doubles as the input's `name`, its `id`, and the key errors arrive under. */
  name: string
  label: string
  /** Standing guidance -- a length rule. Described to screen readers too. */
  hint?: string
  /** The server's message for this field, if it rejected it. */
  error?: string
}

/**
 * One labelled input, wired for a screen reader.
 *
 * The three things that make it usable are all easy to omit and invisible
 * when missing: a real `<label for>` (so the field is announced and its label
 * is a click target), `aria-invalid` (so rejection is announced, not merely
 * coloured), and `aria-describedby` pointing at the hint and the error (so
 * the *reason* is read out). `aria-invalid` says that something is wrong;
 * only the description says what.
 */
export function AuthField({ name, label, hint, error, ...rest }: AuthFieldProps) {
  const hintId = hint === undefined ? undefined : `${name}-hint`
  const describedBy = [hintId, error === undefined ? undefined : errorId(name)]
    .filter((id) => id !== undefined)
    .join(' ')

  return (
    <div className={styles.field}>
      <label className={styles.label} htmlFor={name}>
        {label}
      </label>

      <Input
        id={name}
        name={name}
        invalid={error !== undefined}
        aria-describedby={describedBy === '' ? undefined : describedBy}
        {...rest}
      />

      {hint !== undefined && (
        <p className={styles.hint} id={hintId}>
          {hint}
        </p>
      )}

      {error !== undefined && (
        <p className={styles.fieldError} id={errorId(name)}>
          {error}
        </p>
      )}
    </div>
  )
}
