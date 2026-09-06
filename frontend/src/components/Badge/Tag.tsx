import type { CSSProperties } from 'react'

import { CloseIcon } from '../icons'
import { cx } from '../cx'
import styles from './Badge.module.css'

export interface TagProps {
  /** The label's name. Also what the remove control is named after. */
  name: string
  /**
   * The label's colour, as the server stores it. Used only for the dot -- see
   * the note on `.dot` in Badge.module.css for why it never reaches the text.
   */
  color?: string
  /** Supply to make the tag removable. Absent means the tag is static. */
  onRemove?: () => void
  className?: string
}

/**
 * An entity the user attached: a label, a filter, a selected value.
 *
 * The remove control is a real `<button>` inside the tag rather than the tag
 * itself being clickable. Clicking a tag usually means "filter by this", and a
 * single click target cannot mean both that and "detach this" -- which is how
 * people delete labels they meant to filter by.
 */
export function Tag({ name, color, onRemove, className }: TagProps) {
  const style =
    color === undefined ? undefined : ({ '--tag-color': color } as CSSProperties)

  return (
    <span className={cx(styles.badge, className)} style={style}>
      {color !== undefined && <span className={styles.dot} aria-hidden="true" />}
      <span className={styles.label}>{name}</span>
      {onRemove !== undefined && (
        <button
          type="button"
          className={styles.remove}
          onClick={onRemove}
          aria-label={`Remove ${name}`}
        >
          <CloseIcon />
        </button>
      )}
    </span>
  )
}
