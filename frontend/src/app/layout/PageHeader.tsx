import type { ReactNode } from 'react'

import styles from './PageHeader.module.css'

export interface PageHeaderProps {
  /** The page's name. Becomes the page's only `<h1>`. */
  title: ReactNode
  /** Optional one-line subtitle. Truncates; it is not a place for prose. */
  description?: ReactNode
  /** Page-scoped controls, rendered at the trailing edge. */
  actions?: ReactNode
}

/**
 * The bar at the top of a screen: title, optional subtitle, page actions.
 *
 * Rendered by the *page* rather than by `AppLayout`, which is a deliberate
 * inversion of what the shell diagram suggests. The alternative -- the layout
 * renders the header and pages push a title into it through context or a
 * portal -- puts a screen's title one indirection away from the screen, makes
 * it arrive a render late, and turns "what is this page called" into a
 * question you answer by tracing a subscription. Exporting a component keeps
 * the title in the JSX of the screen that owns it, and the shell still
 * controls every pixel of how it looks.
 *
 * The contract that comes with that:
 *
 *   - `AppLayout` owns the `<main>` landmark. A page must NOT render its own
 *     `<main>`; nested `main` elements are invalid and produce two "main"
 *     landmarks, which defeats the landmark navigation the shell is built
 *     around. Render this and a fragment.
 *   - The title becomes the page's `<h1>`. There is exactly one per screen,
 *     it is this one, and headings inside the page start at `<h2>`. Vector's
 *     baseline stylesheet decouples heading size from heading level for
 *     precisely this reason -- level is structure, size is styling, and a
 *     level must never be picked for how big it looks.
 */
export function PageHeader({ title, description, actions }: PageHeaderProps) {
  return (
    <header className={styles.header}>
      <div className={styles.titleBlock}>
        <h1 className={styles.title}>{title}</h1>
        {description !== undefined && (
          <p className={styles.description}>{description}</p>
        )}
      </div>

      {actions !== undefined && <div className={styles.actions}>{actions}</div>}
    </header>
  )
}
