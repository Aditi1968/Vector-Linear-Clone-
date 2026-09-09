import { useId } from 'react'
import type { DragEvent } from 'react'

import { StatusIndicator, statusCategoryFrom } from '../../../components'
import type { IssueRowFields, WorkflowState, WorkspaceMember } from '../../issues/api'
import styles from '../board.module.css'
import type { BoardColumn as BoardColumnData } from '../lib/arrange'
import { BoardCard, CARD_MIME } from './BoardCard'

export interface BoardColumnProps {
  column: BoardColumnData
  /** Every workflow state a card here can be moved into, in the team's order. */
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
  /** Whether matching issues exist on a page the board has not loaded. */
  isPartial: boolean
}

/**
 * One column of the board.
 *
 * A `<section>` labelled by its own heading, so a screen-reader user can list
 * the columns and jump between them -- which is how you navigate a board
 * without seeing it. The cards are a real `<ul>`, so the count is announced
 * and the whole column can be skipped.
 *
 * ## The count says "loaded" only while it has to
 *
 * A bare "12" beside a heading reads as "there are twelve", and on a board
 * whose second page has not arrived that is false -- so while `isPartial` the
 * number is qualified. Once every matching issue is loaded it is a plain
 * count of what is in the column, which is exactly what it claims to be.
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
  isPartial,
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
      // The channel bar under the legend is tinted from this, in CSS. The
      // category and not a colour: a component that computed the hue would
      // have to be taught the palette, and a column grouped by assignee has
      // no category to state -- so it states nothing and keeps the neutral
      // rule rather than picking a status it is not.
      data-category={category ?? undefined}
      data-droppable={isDroppable ? '' : undefined}
      onDragOver={handleDragOver}
      onDrop={handleDrop}
    >
      {/*
        `aria-labelledby` points at the name alone and not at the whole
        heading, which is what keeps the column's accessible name "In review"
        rather than "Status: In review In review 4 loaded" -- the glyph
        announces itself, and a name that carries a count changes every time a
        card moves.
      */}
      <h2 className={styles.columnHead}>
        {category !== null && <StatusIndicator category={category} name={state?.name} />}
        <span className={styles.columnName} id={headingId}>
          {column.name}
        </span>
        <span className={styles.columnCount}>
          {count}
          {isPartial ? ' loaded' : ''}
        </span>
      </h2>

      {count === 0 ? (
        /* Not an error and not an empty state illustration -- a column with
         * nothing in it is the ordinary condition of half of every board. */
        <p className={styles.columnEmpty}>
          {isPartial
            ? 'Nothing here yet. There may be more on later pages.'
            : 'Nothing here.'}
        </p>
      ) : (
        <ul aria-label={column.name} className={styles.cards} role="list">
          {column.issues.map((issue) => (
            <BoardCard
              assignee={
                issue.assigneeId === null ? undefined : memberById.get(issue.assigneeId)
              }
              // This column is a workflow state, so the columns are the states
              // -- which is what makes dragging and the arrow keys mean
              // something. See `BoardCardProps.columnsAreStates`.
              columnsAreStates={state !== null}
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
