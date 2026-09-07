import type { ReactNode } from 'react'

import { BlockedIcon } from '../icons'
import { cx } from '../cx'
import styles from './States.module.css'

export interface PermissionStateProps {
  /** Overrides the default heading. Keep it about access, never about data. */
  title?: string
  /** Overrides the default sentence. Read the warning below before you do. */
  description?: string
  /** The way out: "Back to issues", "Ask an admin". */
  actions?: ReactNode
  className?: string
}

/**
 * The viewer may not see this.
 *
 * ## The copy is the security boundary, not the component
 *
 * The default wording never says whether the thing exists. That is not
 * timidity -- "you do not have permission to view this project" confirms the
 * project is real to anyone who can type a URL, which turns a list of guessed
 * ids into a membership oracle. Vector's rule (see CLAUDE.md) is that a
 * resource which does not exist and one the caller may not see must be
 * externally indistinguishable wherever the difference would leak existence,
 * and the server already answers NOT_FOUND identically for both.
 *
 * So there are two cases and only one of them is this component:
 *
 *   - The caller is *inside* something they can see and has hit a boundary
 *     within it -- a member opening workspace settings that only admins may
 *     change, say. Membership is already established, the restriction is
 *     about a role rather than about existence, and naming it is helpful.
 *     That is this.
 *   - The caller asked about something they may not see *at all*. There is
 *     nothing to distinguish and nothing safe to say: render the same
 *     not-found screen an absent resource would produce. That is `NotFound`,
 *     not this.
 *
 * A `description` you pass overrides the safe default, so pass one only in
 * the first case, and keep it about the permission rather than about what is
 * behind it.
 *
 * ## No `role`
 *
 * Deliberate, and the same reasoning as `EmptyState`. This replaces a region
 * the user navigated to; it is the content of the page rather than an event
 * that happened to it, so it is read in order like any other content.
 * `role="alert"` would interrupt whatever they were doing to announce a
 * screen they are already looking at.
 */
export function PermissionState({
  title = 'You do not have access',
  description = 'Your role in this workspace does not include this. An admin can change that.',
  actions,
  className,
}: PermissionStateProps) {
  return (
    <div className={cx(styles.state, className)}>
      <div className={styles.glyph}>
        <BlockedIcon />
      </div>
      <p className={styles.title}>{title}</p>
      <p className={styles.description}>{description}</p>
      {actions !== undefined && <div className={styles.actions}>{actions}</div>}
    </div>
  )
}
