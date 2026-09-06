import type { ReactNode } from 'react'

import { cx } from '../cx'
import styles from './Kbd.module.css'

export interface KbdProps {
  children: ReactNode
  className?: string
}

/**
 * One key.
 *
 * A real `<kbd>`, which is the element HTML already has for this and which
 * some screen readers announce differently from surrounding prose.
 *
 * One key per element: a chord is `<Kbd>Ctrl</Kbd>+<Kbd>K</Kbd>`, with the
 * separator supplied by the caller. Baking the separator in would mean picking
 * between "+", "then" and "or" -- which are three different instructions -- and
 * the caller is the only one that knows which it means.
 */
export function Kbd({ children, className }: KbdProps) {
  return <kbd className={cx(styles.kbd, className)}>{children}</kbd>
}
