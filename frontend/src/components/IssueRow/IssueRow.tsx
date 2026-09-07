import type { ReactNode } from 'react'

import { cx } from '../cx'
import { PriorityIndicator, StatusIndicator } from '../indicators'
import type { PriorityLevel, StatusCategory } from '../indicators'
import { ListRow } from '../List'
import styles from './IssueRow.module.css'

export interface IssueRowProps {
  /** The priority glyph's magnitude. Omitted renders an empty column. */
  priority?: PriorityLevel
  /** What the priority is called, for the glyph's accessible name. */
  priorityName?: string
  /**
   * The workflow state's category, which decides the glyph's silhouette.
   *
   * Optional because a caller that has not resolved the issue's state yet
   * should render nothing rather than guess: a plausible-looking backlog ring
   * for a state we could not look up is worse than a gap.
   */
  status?: StatusCategory
  /** The state's own name -- "In review" -- for the glyph's accessible name. */
  statusName?: string
  /** The mono key: `ENG-142`. */
  identifier: string
  /**
   * The issue's title, and the row's single interactive element.
   *
   * Pass a link -- `<Link to={paths.issue(id)}>{title}</Link>` -- and this
   * component stretches it over the whole row, so clicking anywhere follows
   * it. See the stylesheet for why that is done in CSS rather than by making
   * the row a button.
   */
  title: ReactNode
  /** Labels and the project chip: the row's fifth column. */
  labels?: ReactNode
  /** Due date, age and assignee: the trailing column. */
  meta?: ReactNode
  /** This row is the one an inspector is showing. */
  selected?: boolean
  className?: string
}

/**
 * One issue, as a row in a list.
 *
 * Six columns on a grid rather than a flex line, with the leading three at
 * fixed widths. That is what makes every title down a list start on the same
 * x, and the alignment is most of what makes a dense list scannable. It also
 * survives a row whose workflow state has not resolved, where a flex line
 * would shuffle the whole row left.
 *
 * The key column is `--key-column` and not `auto` on purpose. Each row is its
 * own grid container, so `auto` would size that track to *that row's* key --
 * and a list holding both `ENG-9` and `ENG-1421` would start its titles at
 * two different x positions, which is the exact thing the fixed tracks exist
 * to prevent.
 *
 * ## Presentation only
 *
 * No GraphQL type, no router, no data. Callers hand it rendered pieces --
 * a `<Tag>` list, an `<Avatar>`, a `<time>` -- which is what lets triage,
 * favourites, saved views and a team's issues all draw the same row from four
 * different queries. The row's *shape* is the shared thing; what fills it is
 * the feature's business.
 */
export function IssueRow({
  priority,
  priorityName,
  status,
  statusName,
  identifier,
  title,
  labels,
  meta,
  selected = false,
  className,
}: IssueRowProps) {
  return (
    <ListRow interactive selected={selected} className={cx(styles.row, className)}>
      {priority === undefined ? (
        <span aria-hidden="true" />
      ) : (
        <PriorityIndicator level={priority} name={priorityName} />
      )}

      {status === undefined ? (
        <span aria-hidden="true" />
      ) : (
        <StatusIndicator category={status} name={statusName} />
      )}

      <span className={styles.identifier}>{identifier}</span>

      <span className={styles.title}>{title}</span>

      <span className={styles.labels}>{labels}</span>

      <span className={styles.meta}>{meta}</span>
    </ListRow>
  )
}
