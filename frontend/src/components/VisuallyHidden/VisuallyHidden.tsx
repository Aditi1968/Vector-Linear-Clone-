import type { ReactNode } from 'react'

import { cx } from '../cx'
import styles from './VisuallyHidden.module.css'

export interface VisuallyHiddenProps {
  children: ReactNode
  /**
   * The element to render. Defaults to `<span>`; pass `'div'` when the hidden
   * text sits between block-level siblings, since an inline element there
   * would be wrapped in an anonymous block by some engines.
   */
  as?: 'span' | 'div'
  className?: string
}

/**
 * Content available to screen readers and absent from the visual design.
 *
 * The legitimate use is text that a sighted user gets from *layout* and a
 * screen-reader user cannot: the target of a skip link, or the clarifying
 * half of a control whose visible label is a single ambiguous word.
 *
 * It is not a way to add explanations that the visual design should have
 * carried. If sighted users need the information too, it belongs on screen.
 */
export function VisuallyHidden({
  children,
  as: Element = 'span',
  className,
}: VisuallyHiddenProps) {
  return (
    <Element className={cx(styles.visuallyHidden, className)}>{children}</Element>
  )
}
