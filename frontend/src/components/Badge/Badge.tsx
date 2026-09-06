import type { ReactNode } from 'react'

import { cx } from '../cx'
import styles from './Badge.module.css'

export type BadgeTone =
  | 'neutral'
  | 'accent'
  | 'success'
  | 'warning'
  | 'danger'
  | 'info'

export interface BadgeProps {
  tone?: BadgeTone
  children: ReactNode
  className?: string
}

/**
 * A short, static statement about the thing beside it.
 *
 * A badge is not interactive and does not announce itself as anything -- it is
 * text with a box around it, and its meaning has to be readable in the text
 * itself. `tone` may reinforce that meaning but must never be the only place
 * it lives: a red badge saying "42" tells a colourblind user nothing that a
 * grey one does not.
 */
export function Badge({ tone = 'neutral', children, className }: BadgeProps) {
  return (
    <span
      className={cx(styles.badge, tone !== 'neutral' && styles[tone], className)}
    >
      <span className={styles.label}>{children}</span>
    </span>
  )
}
