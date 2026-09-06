import type { ComponentPropsWithRef } from 'react'

import { ChevronDownIcon } from '../icons'
import { cx } from '../cx'
import styles from './Field.module.css'
import type { FieldSize } from './Input'

export interface SelectProps extends Omit<ComponentPropsWithRef<'select'>, 'size'> {
  size?: FieldSize
  /** See `InputProps.invalid`. */
  invalid?: boolean
}

/**
 * A native `<select>` in Vector's clothes.
 *
 * The chevron is a sibling element rather than a `background-image`, because a
 * background image cannot be `currentColor` and would therefore need a second
 * copy per theme and a third for the disabled state.
 */
export function Select({
  size = 'md',
  invalid = false,
  className,
  children,
  ...rest
}: SelectProps) {
  return (
    <span className={styles.root}>
      <select
        aria-invalid={invalid || undefined}
        className={cx(styles.control, styles[size], styles.select, className)}
        {...rest}
      >
        {children}
      </select>
      <span className={styles.chevron}>
        <ChevronDownIcon />
      </span>
    </span>
  )
}
