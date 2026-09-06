import type { ComponentPropsWithRef } from 'react'

import { cx } from '../cx'
import styles from './Field.module.css'

export interface TextareaProps extends ComponentPropsWithRef<'textarea'> {
  /** See `InputProps.invalid`. */
  invalid?: boolean
}

/**
 * Multi-line text: issue descriptions, comments.
 *
 * Shares `.control` with `Input` so the two match exactly when stacked, and
 * overrides only the three things that genuinely differ: auto height, vertical
 * padding, and vertical-only resizing.
 */
export function Textarea({ invalid = false, className, ...rest }: TextareaProps) {
  return (
    <textarea
      aria-invalid={invalid || undefined}
      className={cx(styles.control, styles.md, styles.textarea, className)}
      {...rest}
    />
  )
}
