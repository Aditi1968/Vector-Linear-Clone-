import { Link } from 'react-router-dom'

import { useAppPaths } from '../../../app/routes'
import {
  Avatar,
  ListRow,
  PriorityIndicator,
  ProjectIcon,
  statusCategoryFrom,
  StatusIndicator,
  Tag,
  VisuallyHidden,
} from '../../../components'
import { memberLabel } from '../api'
import type { IssueRowFields, WorkflowState, WorkspaceMember } from '../api'
import styles from '../issues.module.css'
import { formatDueDate } from '../lib/dates'
import { describePriority, priorityLevel } from '../lib/priority'

/** How many labels fit on a row before the rest become a count. */
const VISIBLE_LABELS = 3

export interface IssueRowProps {
  issue: IssueRowFields
  /**
   * The state `issue.workflowStateId` names, resolved by the caller.
   *
   * A prop and not a lookup, because the lookup is one query for the whole
   * screen (`useWorkspaceContext`) and a row that ran it would run it 25
   * times per page. `undefined` while the context is still loading, which
   * renders as an empty status column rather than as a guess.
   */
  state: WorkflowState | undefined
  /** The person `issue.assigneeId` names. `undefined` means unassigned. */
  assignee: WorkspaceMember | undefined
  /** This row is the one the inspector is showing. */
  selected: boolean
  /** Today, as `YYYY-MM-DD`, for deciding whether the due date has passed. */
  today: string
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
 * It is also the row's *only* interactive element, deliberately. A project
 * chip that was itself a link would double the tab stops on every row and
 * would make "the number of links in the list" stop meaning "the number of
 * issues".
 *
 * ## Seven columns, and every one of them fixed except the title
 *
 * Priority, status, key, title, labels, due, assignee -- the design's own
 * seven. The grid lives in `issues.module.css` as `.rowGrid` and the column
 * header composes the same declaration, which is what keeps DUE over the
 * dates. See that file for why none of the trailing tracks may be `auto`.
 *
 * Cycle, estimate and `updatedAt` used to ride in a trailing flex cluster and
 * no longer do. A cluster is as wide as its widest row, so nothing above it
 * can be labelled; all three are on the detail panel, and
 * `components/IssueRow` still offers a free-form meta slot for the screens
 * that want them in a row.
 *
 * The destination comes from `useAppPaths()` and never from a template
 * literal, which is the rule `src/app/routes/paths.ts` exists to enforce.
 */
export function IssueRow({
  issue,
  state,
  assignee,
  selected,
  today,
}: IssueRowProps) {
  const paths = useAppPaths()
  const category = state === undefined ? null : statusCategoryFrom(state.category)
  const { name: priorityName } = describePriority(issue.priority)
  const labels = issue.labels.slice(0, VISIBLE_LABELS)
  const hiddenLabelCount = issue.labels.length - labels.length

  // A due date in the past only matters while the issue is still open;
  // "overdue" on something already completed is a fact about history.
  const isOverdue =
    issue.dueDate !== null &&
    issue.completedAt === null &&
    issue.dueDate < today

  return (
    <ListRow interactive selected={selected} className={styles.rowItem}>
      <Link
        aria-current={selected ? 'page' : undefined}
        className={styles.row}
        data-issue-row=""
        to={paths.issue(issue.id)}
      >
        <PriorityIndicator
          level={priorityLevel(issue.priority)}
          name={priorityName ?? String(issue.priority)}
        />

        {/* An empty cell rather than a fallback glyph when the workflow
          * state is not known yet: a plausible-looking backlog square for a
          * state we could not resolve is worse than a gap. */}
        {category === null ? (
          <span aria-hidden="true" />
        ) : (
          <StatusIndicator category={category} name={state?.name} />
        )}

        <span className={styles.identifier}>{issue.identifier}</span>

        <span className={styles.rowTitle}>{issue.title}</span>

        {/*
          Always rendered, even with nothing in it. Every row is its own grid
          container, so a row that omitted this cell would put its meta column
          in track 5 and leave track 6 empty -- and the gap before that empty
          track moves the trailing edge of an unlabelled row out of line with
          a labelled one.

          The project sits here rather than in the meta column because that is
          where the design puts it: what an issue is *about* belongs beside its
          labels, and the trailing column is reserved for when and who.
        */}
        <span className={styles.rowLabels}>
          {labels.map((label) => (
            <Tag color={label.color} key={label.id} name={label.name} />
          ))}
          {hiddenLabelCount > 0 && (
            <span className={styles.labelOverflow}>+{hiddenLabelCount}</span>
          )}

          {issue.project !== null && (
            <span className={styles.chip}>
              <ProjectIcon />
              <VisuallyHidden>Project</VisuallyHidden>
              {issue.project.name}
            </span>
          )}
        </span>

        {/* The cell is here whether or not there is a date in it. A grid puts
          * the next child in the next free track, so omitting it would slide
          * the assignee into the due column and put every avatar down the
          * list on two different x positions. */}
        <span className={styles.rowDue}>
          {issue.dueDate !== null && (
            <time
              className={styles.due}
              data-overdue={isOverdue ? '' : undefined}
              dateTime={issue.dueDate}
            >
              <VisuallyHidden>{isOverdue ? 'Overdue' : 'Due'}</VisuallyHidden>
              {formatDueDate(issue.dueDate)}
            </time>
          )}
        </span>

        {assignee === undefined ? (
          <span className={styles.unassigned}>
            <VisuallyHidden>Unassigned</VisuallyHidden>
          </span>
        ) : (
          <Avatar
            className={styles.rowAssignee}
            name={memberLabel(assignee)}
            size="sm"
          />
        )}
      </Link>
    </ListRow>
  )
}
