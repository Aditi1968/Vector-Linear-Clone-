import { Link } from 'react-router-dom'
import type { DragEvent, KeyboardEvent } from 'react'

import { useAppPaths } from '../../../app/routes'
import {
  Avatar,
  CycleIcon,
  Menu,
  PriorityIndicator,
  ProjectIcon,
  StatusIndicator,
  Tag,
  VisuallyHidden,
  statusCategoryFrom,
} from '../../../components'
import type { MenuItem } from '../../../components'
import { memberLabel } from '../../issues/api'
import type { IssueRowFields, WorkflowState, WorkspaceMember } from '../../issues/api'
import { formatDueDate } from '../../issues/lib/dates'
import { describePriority, priorityLevel } from '../../issues/lib/priority'
import styles from '../board.module.css'

/** How many labels fit on a card before the rest become a count. */
const VISIBLE_LABELS = 3

/** The MIME type a dragged card carries. `text/plain` is the one every browser agrees on. */
export const CARD_MIME = 'text/plain'

export interface BoardCardProps {
  issue: IssueRowFields
  /** The person `issue.assigneeId` names. `undefined` means unassigned. */
  assignee: WorkspaceMember | undefined
  /** Today as `YYYY-MM-DD`, for deciding whether the due date has passed. */
  today: string
  /**
   * Every column this card could move to, in board order.
   *
   * Empty when the board is not grouped by status, which is what removes the
   * move affordances entirely: a card in an assignee column has no "next
   * column" that a status change would take it to, and offering one would move
   * the card somewhere the user cannot see.
   */
  moveTargets: readonly WorkflowState[]
  onMove: (issue: IssueRowFields, workflowStateId: string) => void
  /** A move is in flight for this card. */
  moving: boolean
  onDragStart: (issueId: string) => void
  onDragEnd: () => void
}

/**
 * One card.
 *
 * ## Two interactive elements, and no more
 *
 * The title is a `<Link>` and the move control is a `<Menu>`. The card itself
 * is not clickable, because a card that was a button could not contain either
 * of them -- nested interactive elements are invalid and the inner one is
 * unreachable by keyboard. That is also why the link is not stretched over the
 * whole card with an `::after`, the trick `components/List` documents: the
 * overlay would sit on top of the menu button and swallow its clicks.
 *
 * ## Moving a card without a mouse
 *
 * Two paths, and the keyboard one is not an afterthought:
 *
 *   - the menu, which reaches any column by name;
 *   - `Alt` with the left and right arrows on the focused title, which moves
 *     the card one column at a time in board order.
 *
 * `Alt` is what keeps a bare ArrowLeft/ArrowRight free for the caret and for
 * whatever navigation the shell binds, and the modifier is checked rather than
 * assumed -- an unmodified arrow falls through untouched.
 *
 * Neither path announces anything itself. The screen owns the live region,
 * because it is the screen that knows whether the server accepted.
 */
export function BoardCard({
  issue,
  assignee,
  today,
  moveTargets,
  onMove,
  moving,
  onDragStart,
  onDragEnd,
}: BoardCardProps) {
  const paths = useAppPaths()
  const { name: priorityName } = describePriority(issue.priority)
  const labels = issue.labels.slice(0, VISIBLE_LABELS)
  const hiddenLabelCount = issue.labels.length - labels.length

  // A due date in the past only matters while the issue is still open;
  // "overdue" on something already completed is a fact about history.
  const isOverdue =
    issue.dueDate !== null && issue.completedAt === null && issue.dueDate < today

  const currentIndex = moveTargets.findIndex(
    (state) => state.id === issue.workflowStateId,
  )

  const items: MenuItem[] = moveTargets.map((state) => {
    const category = statusCategoryFrom(state.category)

    return {
      id: state.id,
      label: state.name,
      // Wrapped in an `aria-hidden` span rather than passed bare: the
      // indicator is a `role="img"` with its own name, and inside a menu item
      // it would announce "Status: Todo, Todo".
      icon:
        category === null ? undefined : (
          <span aria-hidden="true">
            <StatusIndicator category={category} name={state.name} />
          </span>
        ),
      // The column it is already in. Left visible and disabled rather than
      // filtered out, so the menu is the same shape every time it opens and
      // says where the card currently is.
      disabled: state.id === issue.workflowStateId,
      onSelect: () => {
        onMove(issue, state.id)
      },
    }
  })

  function handleKeyDown(event: KeyboardEvent<HTMLAnchorElement>) {
    if (!event.altKey || (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight')) {
      return
    }

    // The card's own state is not among the targets -- a state belonging to
    // another team, or a lookup that has not answered. There is no "one
    // column over" from a position that is not on the board.
    if (currentIndex === -1) {
      return
    }

    const target = moveTargets[currentIndex + (event.key === 'ArrowRight' ? 1 : -1)]

    // No wrapping. At either end the keystroke does nothing, because a card
    // silently reappearing at the opposite side of the board is the one thing
    // a keyboard user cannot see happen.
    if (target === undefined) {
      return
    }

    event.preventDefault()
    onMove(issue, target.id)
  }

  function handleDragStart(event: DragEvent<HTMLLIElement>) {
    // The id and not the whole issue: what crosses a drag is a string, and the
    // screen already holds every loaded card to look it up in.
    event.dataTransfer.setData(CARD_MIME, issue.id)
    event.dataTransfer.effectAllowed = 'move'
    onDragStart(issue.id)
  }

  return (
    <li
      className={styles.card}
      data-moving={moving ? '' : undefined}
      // Dragging is an enhancement on top of the menu and the arrows, and it
      // is off entirely unless a drop would mean something -- which is only
      // when the columns are workflow states.
      draggable={moveTargets.length > 0}
      onDragEnd={onDragEnd}
      onDragStart={handleDragStart}
    >
      <div className={styles.cardHead}>
        <PriorityIndicator
          level={priorityLevel(issue.priority)}
          name={priorityName ?? String(issue.priority)}
        />

        <span className={styles.identifier}>{issue.identifier}</span>

        {moveTargets.length > 0 && (
          <Menu
            align="end"
            className={styles.cardMenu}
            items={items}
            label={`Move ${issue.identifier}`}
          />
        )}
      </div>

      <Link
        className={styles.cardTitle}
        data-board-card=""
        onKeyDown={handleKeyDown}
        to={paths.issue(issue.id)}
      >
        {issue.title}
      </Link>

      {issue.labels.length > 0 && (
        <span className={styles.cardLabels}>
          {labels.map((label) => (
            <Tag color={label.color} key={label.id} name={label.name} />
          ))}
          {hiddenLabelCount > 0 && (
            <span className={styles.labelOverflow}>+{hiddenLabelCount}</span>
          )}
        </span>
      )}

      <div className={styles.cardMeta}>
        {issue.project !== null && (
          <span className={styles.chip}>
            <ProjectIcon />
            <VisuallyHidden>Project</VisuallyHidden>
            {issue.project.name}
          </span>
        )}

        {issue.cycle !== null && (
          <span className={styles.chip}>
            <CycleIcon />
            <VisuallyHidden>Cycle</VisuallyHidden>
            {issue.cycle.name ?? `Cycle ${String(issue.cycle.number)}`}
          </span>
        )}

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

        {issue.estimate !== null && (
          <span className={styles.estimate}>
            <VisuallyHidden>Estimate</VisuallyHidden>
            {issue.estimate}
          </span>
        )}

        <span className={styles.cardAssignee}>
          {assignee === undefined ? (
            <VisuallyHidden>Unassigned</VisuallyHidden>
          ) : (
            <Avatar name={memberLabel(assignee)} size="sm" />
          )}
        </span>
      </div>
    </li>
  )
}
