import { Link } from 'react-router-dom'

import { useAppPaths } from '../../../app/routes'
import type { IssueRowFields } from '../api'
import styles from '../issues.module.css'
import { formatAbsolute, formatRelative } from '../lib/dates'
import { PriorityBadge } from './PriorityBadge'

export interface IssueRowProps {
  issue: IssueRowFields
}

/**
 * One row of the issue list.
 *
 * The whole row is a `<Link>`, which is what makes it keyboard-operable
 * without a single key handler: Tab reaches it because a link with an href is
 * natively focusable, Enter follows it because that is what Enter does on a
 * link, and middle-click and "open in new tab" work because it really is a
 * link. A `<div role="link" tabIndex={0} onKeyDown={...}>` would need all of
 * that written out and would still get the middle-click case wrong.
 *
 * The destination comes from `useAppPaths()` and never from a template
 * literal, which is the rule `src/app/routes/paths.ts` exists to enforce:
 * when routes gain a `/:workspaceSlug` segment, this component does not
 * change.
 *
 * Every field rendered here is one the server sends. There is no status, no
 * assignee, no `VEC-42` identifier and no placeholder standing in for one.
 */
export function IssueRow({ issue }: IssueRowProps) {
  const paths = useAppPaths()

  return (
    <li className={styles.listItem}>
      <Link className={styles.row} to={paths.issue(issue.id)}>
        <PriorityBadge value={issue.priority} />

        <span className={styles.rowTitle}>{issue.title}</span>

        {/*
          Rendered only when the server actually has a completion timestamp.
          It is null for every issue today -- the schema exposes no mutation
          that sets it -- and an always-present "Not completed" chip would be
          a column of noise describing a feature that does not exist.
        */}
        {issue.completedAt !== null && (
          <span
            className={styles.rowCompleted}
            title={`Completed ${formatAbsolute(issue.completedAt)}`}
          >
            Completed
          </span>
        )}

        {/*
          Relative for scanning, exact on hover and in the accessible name.
          `updatedAt` is not shown: no mutation updates an issue, so it equals
          `createdAt` for every row, and a second identical timestamp reads as
          information when it is not.
        */}
        <span className={styles.rowMeta}>
          <time dateTime={issue.createdAt} title={formatAbsolute(issue.createdAt)}>
            {formatRelative(issue.createdAt)}
          </time>
        </span>
      </Link>
    </li>
  )
}
