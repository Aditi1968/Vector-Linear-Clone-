import type { ButtonHTMLAttributes, ReactNode } from 'react'

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

export type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> &
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
  type = 'button',
  className,
  children,
  ...rest
}: ButtonProps) {
  const isIconOnly = children === undefined

  return (
    <button
      type={type}
      className={cx(
        styles.button,
        styles[variant],
        styles[size],
        isIconOnly && styles.iconOnly,
        fullWidth && styles.fullWidth,
        className,
      )}
      {...rest}
    >
      {icon !== undefined && <span className={styles.icon}>{icon}</span>}
      {!isIconOnly && <span className={styles.label}>{children}</span>}
    </button>
  )
}
