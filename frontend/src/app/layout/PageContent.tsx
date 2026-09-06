import type { ReactNode } from 'react'

import { cx } from '../../components'
import styles from './PageContent.module.css'

export interface PageContentProps {
  children: ReactNode
  /**
   * Cap the content at a comfortable reading measure. Off by default -- a
   * dense list should use the width it is given.
   */
  constrained?: boolean
  className?: string
}

/**
 * The padded region below a `PageHeader`.
 *
 * It exists so that the shell's horizontal rhythm is defined once. `PageHeader`
 * is full-bleed and pads itself; if each screen then chose its own content
 * padding, a page's title and its first row would not line up, and the
 * misalignment is the kind that looks like nothing in isolation and like
 * carelessness across three screens.
 *
 * `<div>`, not `<main>` or `<section>`. `AppLayout` already provides the
 * `main` landmark, and a `<section>` without an accessible name adds a
 * landmark that announces as bare "region" -- noise for a screen-reader user
 * navigating by landmark.
 */
export function PageContent({ children, constrained = false, className }: PageContentProps) {
  return (
    <div className={cx(styles.content, constrained && styles.constrained, className)}>
      {children}
    </div>
  )
}
