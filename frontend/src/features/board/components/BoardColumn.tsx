import { useId } from 'react'
import type { DragEvent } from 'react'

import { StatusIndicator, statusCategoryFrom } from '../../../components'
import type { IssueRowFields, WorkflowState, WorkspaceMember } from '../../issues/api'
import styles from '../board.module.css'
import type { BoardColumn as BoardColumnData } from '../lib/arrange'
import { BoardCard, CARD_MIME } from './BoardCard'

export interface BoardColumnProps {
  column: BoardColumnData
  /** Every column this board can move a card to. Empty unless grouped by status. */
  moveTargets: readonly WorkflowState[]
  memberById: ReadonlyMap<string, WorkspaceMember>
  today: string
  onMove: (issue: IssueRowFields, workflowStateId: string) => void
  /** The card being dragged right now, if any. */
  draggingId: string | null
  onDragStart: (issueId: string) => void
  onDragEnd: () => void
  /** A card was dropped here. The screen resolves the id and performs the move. */
  onDrop: (issueId: string, workflowStateId: string) => void
  /** The id of the card whose move is in flight, if any. */
  movingId: string | null
}

/**
 * One column of the board.
 *
 * A `<section>` labelled by its own heading, so a screen-reader user can list
 * the columns and jump between them -- which is how you navigate a board
 * without seeing it. The cards are a real `<ul>`, so the count is announced
 * and the whole column can be skipped.
 *
 * ## The count says "loaded", every time
 *
 * Not decoration and not hedging. `issues(workspaceSlug:, teamId:, first:,
 * after:)` has no filter and no aggregate, so the only number this screen can
 * honestly show is how many of the cards it has fetched are in this column.
 * A bare "12" beside a heading reads as "there are twelve", and on a board
 * whose second page has not loaded that is false.
 *
 * ## Dropping
 *
 * `onDragOver` must call `preventDefault()` or the browser refuses the drop --
 * that is the HTML5 drag-and-drop contract, and it is the single most common
 * reason a hand-rolled board silently does nothing. It is called only when
 * this column is a workflow state, so the other groupings are inert rather
 * than accepting a drop that could not mean anything.
 */
export function BoardColumn({
  column,
  moveTargets,
  memberById,
  today,
  onMove,
  draggingId,
  onDragStart,
  onDragEnd,
  onDrop,
  movingId,
}: BoardColumnProps) {
  const headingId = useId()
  const state = column.state
  const category = state === null ? null : statusCategoryFrom(state.category)
  const count = column.issues.length
  const isDroppable = state !== null && draggingId !== null

  function handleDragOver(event: DragEvent<HTMLElement>) {
    if (!isDroppable) {
      return
    }

    // Without this the drop never fires. See the note above.
    event.preventDefault()
    event.dataTransfer.dropEffect = 'move'
  }

  function handleDrop(event: DragEvent<HTMLElement>) {
    if (state === null) {
      return
    }

    event.preventDefault()
    const issueId = event.dataTransfer.getData(CARD_MIME)

    if (issueId.length > 0) {
      onDrop(issueId, state.id)
    }
  }

  return (
    <section
      aria-labelledby={headingId}
      className={styles.column}
      data-droppable={isDroppable ? '' : undefined}
      onDragOver={handleDragOver}
      onDrop={handleDrop}
    >
      <h2 className={styles.columnHead} id={headingId}>
        {category !== null && <StatusIndicator category={category} name={state?.name} />}
        <span className={styles.columnName}>{column.name}</span>
        <span className={styles.columnCount}>{count} loaded</span>
      </h2>

      {count === 0 ? (
        /* Not an error and not an empty state illustration -- a column with
         * nothing in it is the ordinary condition of half of every board. The
         * sentence says which of the two reasons it might be, because from
         * inside the browser they are indistinguishable. */
        <p className={styles.columnEmpty}>
          No loaded issues here. There may be more on later pages.
        </p>
      ) : (
        <ul aria-label={column.name} className={styles.cards} role="list">
          {column.issues.map((issue) => (
            <BoardCard
              assignee={
                issue.assigneeId === null ? undefined : memberById.get(issue.assigneeId)
              }
              issue={issue}
              key={issue.id}
              moveTargets={moveTargets}
              moving={issue.id === movingId}
              onDragEnd={onDragEnd}
              onDragStart={onDragStart}
              onMove={onMove}
              today={today}
            />
          ))}
        </ul>
      )}
    </section>
  )
}
