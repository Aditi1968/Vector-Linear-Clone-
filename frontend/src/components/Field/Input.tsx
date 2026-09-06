import type { ComponentPropsWithRef, ReactNode } from 'react'

import { cx } from '../cx'
import styles from './Field.module.css'

/** Matches `ButtonSize`, so a button beside an input lines up without thought. */
export type FieldSize = 'sm' | 'md'

export interface InputProps extends Omit<ComponentPropsWithRef<'input'>, 'size'> {
  size?: FieldSize
  /**
   * Flags the value as rejected. Writes `aria-invalid`, which is both what a
   * screen reader announces and what the stylesheet keys the red border off --
   * one flag, so the two cannot disagree.
   *
   * Point `aria-describedby` at the element holding the message; `aria-invalid`
   * says *that* something is wrong, never *what*.
   */
  invalid?: boolean
  /** A decorative leading glyph -- a magnifier in a search field. */
  icon?: ReactNode
}

/**
 * A single-line text field.
 *
 * No label here on purpose. A label needs to be positioned by the layout that
 * owns the form, and a component that renders its own has to grow props for
 * every arrangement someone needs; a `<label htmlFor>` beside the input costs
 * the caller one element and stays correct.
 */
export function Input({
  size = 'md',
  invalid = false,
  icon,
  className,
  ...rest
}: InputProps) {
  const input = (
    <input
      aria-invalid={invalid || undefined}
      className={cx(
        styles.control,
        styles[size],
        icon !== undefined && styles.hasAdornment,
        className,
      )}
      {...rest}
    />
  )

  if (icon === undefined) {
    return input
  }

  return (
    <span className={styles.root}>
      {input}
      <span className={styles.adornment}>{icon}</span>
    </span>
  )
}
