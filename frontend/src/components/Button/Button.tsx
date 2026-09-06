import type { ComponentPropsWithRef, MouseEvent, ReactNode } from 'react'

import { Spinner } from '../Spinner'
import { cx } from '../cx'
import styles from './Button.module.css'

export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger'
export type ButtonSize = 'sm' | 'md'

interface ButtonBaseProps {
  variant?: ButtonVariant
  size?: ButtonSize
  /**
   * Leading glyph. Decorative: icons from `components/icons` are
   * `aria-hidden`, and the accessible name comes from `children` or from
   * `aria-label`.
   */
  icon?: ReactNode
  /** Stretch to the width of the container. Used by the sidebar's controls. */
  fullWidth?: boolean
  /**
   * The action this button started has not finished.
   *
   * Swaps the leading glyph for a spinner, marks the button `aria-busy`, and
   * swallows further clicks. See the note on the implementation for why it
   * does *not* set `disabled`.
   */
  loading?: boolean
}

/** A button whose visible text is its accessible name. */
type LabelledButtonProps = ButtonBaseProps & {
  children: ReactNode
}

/**
 * A button with no visible text.
 *
 * `aria-label` is *required* here, and that requirement is the entire reason
 * this type exists as a separate arm of the union. An icon-only button with
 * no accessible name is announced as "button" and is unusable with a screen
 * reader -- and it is the single easiest accessibility bug to ship, because
 * it looks completely correct. Making it a compile error is more reliable
 * than making it a review item.
 */
type IconOnlyButtonProps = ButtonBaseProps & {
  children?: never
  icon: ReactNode
  'aria-label': string
}

export type ButtonProps = Omit<ComponentPropsWithRef<'button'>, 'children'> &
  (LabelledButtonProps | IconOnlyButtonProps)

/**
 * The product's button.
 *
 * Always a real `<button>`. Nothing in Vector may use a `<div>` with an
 * `onClick` in its place: a `<button>` is in the tab order, fires on Enter
 * and Space, is announced with a role, and participates in the global
 * `:focus-visible` ring. Each of those is a separate thing to reimplement and
 * a separate thing to get wrong.
 *
 * `type` defaults to `"button"` rather than the HTML default of `"submit"`.
 * The default is a long-standing footgun: a plain button inside a form
 * submits it, and the bug only appears once the button is later moved into a
 * form -- far from the code that caused it.
 */
export function Button({
  variant = 'secondary',
  size = 'md',
  icon,
  fullWidth = false,
  loading = false,
  type = 'button',
  className,
  children,
  onClick,
  ...rest
}: ButtonProps) {
  const isIconOnly = children === undefined

  /**
   * A loading button is `aria-disabled`, not `disabled`.
   *
   * `disabled` drops the element out of the tab order, and the browser then
   * moves focus to `<body>` -- so a keyboard user who pressed Enter on "Save"
   * loses their place at the exact moment the app starts working, and has to
   * tab back in from the top of the page to find out what happened.
   * `aria-disabled` announces the same state and keeps focus where the user
   * put it; the click handler below is what actually makes it inert.
   */
  const handleClick = (event: MouseEvent<HTMLButtonElement>) => {
    if (loading) {
      // preventDefault stops a `type="submit"` button submitting its form;
      // stopPropagation stops a delegated handler further up acting on it.
      event.preventDefault()
      event.stopPropagation()
      return
    }
    onClick?.(event)
  }

  const leading = loading ? <Spinner /> : icon

  return (
    <button
      type={type}
      onClick={handleClick}
      aria-busy={loading || undefined}
      aria-disabled={loading || undefined}
      className={cx(
        styles.button,
        styles[variant],
        styles[size],
        isIconOnly && styles.iconOnly,
        fullWidth && styles.fullWidth,
        loading && styles.loading,
        className,
      )}
      {...rest}
    >
      {leading !== undefined && <span className={styles.icon}>{leading}</span>}
      {/* The label stays rendered while loading. Replacing it with the spinner
        * would change the button's width mid-click and shift whatever is beside
        * it -- and the word the user just read is the best confirmation of
        * which action is in flight. */}
      {!isIconOnly && <span className={styles.label}>{children}</span>}
    </button>
  )
}
