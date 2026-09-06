import { useState } from 'react'
import { Link } from 'react-router-dom'

import {
  Button,
  EmptyState,
  IconButton,
  CloseIcon,
  PlusIcon,
  RelationIcon,
  RelationIndicator,
  Select,
  relationKindFrom,
} from '../../../components'
import type { RelationKind } from '../../../components'
import { useAppPaths } from '../../../app/routes'
import { describeOutcome, useRelations } from '../api'
import type { IssueRelation, IssueRelationType } from '../api'
import styles from '../collaboration.module.css'
import { IssuePicker } from './IssuePicker'
import { Panel } from './Panel'

export interface RelationsPanelProps {
  workspaceSlug: string
  issueId: string
}

/**
 * The four relation types, in the order they are shown.
 *
 * Blocking first, and "blocked by" before "blocks", because that is the order
 * of how much the reader has to care: what is stopping this issue, then what
 * this issue is stopping, then the two that are only context. A grouping in
 * schema order would put `BLOCKS` above `BLOCKED_BY` for no reason a reader
 * benefits from.
 *
 * The labels are the picker's, not the display's -- `RelationIndicator` names
 * each kind itself, and having a second set of names here is how the two end
 * up disagreeing.
 */
const RELATION_ORDER: readonly { type: IssueRelationType; label: string }[] = [
  { type: 'BLOCKED_BY', label: 'Blocked by' },
  { type: 'BLOCKS', label: 'Blocks' },
  { type: 'RELATED', label: 'Related to' },
  { type: 'DUPLICATE', label: 'Duplicate of' },
]

/**
 * An issue's links to other issues, grouped by what the link means.
 *
 * ## Blocking is not a colour
 *
 * An unresolved `BLOCKED_BY` changes what the reader can do, so it is stated
 * in words at the top of the panel and not left to a tinted glyph. The count
 * only includes blockers that are still open: an issue blocked by something
 * already completed is not blocked, and saying it is would send someone to
 * unblock a finished issue.
 *
 * ## No direction is inverted here
 *
 * The type sent is the type the user chose and the type rendered is the type
 * the server returned. `BLOCKS` and `BLOCKED_BY` are two ends of one edge and
 * the server owns which end this issue is on; a panel that flipped them would
 * show the opposite of the truth on the other issue's screen.
 */
export function RelationsPanel({ workspaceSlug, issueId }: RelationsPanelProps) {
  const {
    relations,
    isLoading,
    errorMessage,
    retry,
    hasNextPage,
    isLoadingMore,
    loadMoreErrorMessage,
    loadMore,
    createRelation,
    deleteRelation,
    isBusy,
  } = useRelations(workspaceSlug, issueId)

  const paths = useAppPaths()
  const [status, setStatus] = useState<string | null>(null)
  const [isPicking, setIsPicking] = useState(false)
  const [pickerError, setPickerError] = useState<string | null>(null)
  const [type, setType] = useState<IssueRelationType>('BLOCKED_BY')

  const openBlockers = relations.filter(
    (relation) => relation.type === 'BLOCKED_BY' && relation.issue.completedAt === null,
  )

  const groups = RELATION_ORDER.map((group) => ({
    ...group,
    kind: relationKindFrom(group.type),
    rows: relations.filter((relation) => relation.type === group.type),
  })).filter((group) => group.rows.length > 0)

  const remove = (relation: IssueRelation) => {
    void deleteRelation(relation.id).then((outcome) => {
      setStatus(
        outcome.status === 'ok'
          ? `Removed the link to ${relation.issue.title}`
          : describeOutcome(outcome),
      )
    })
  }

  return (
    <Panel
      title="Relations"
      count={isLoading ? undefined : relations.length}
      isLoading={isLoading}
      errorMessage={errorMessage}
      onRetry={retry}
      status={status}
      action={
        <Button
          size="sm"
          variant="secondary"
          icon={<PlusIcon />}
          onClick={() => {
            setPickerError(null)
            setIsPicking(true)
          }}
        >
          Add
        </Button>
      }
    >
      {openBlockers.length > 0 && (
        <p className={styles.blocked}>
          <RelationIndicator kind="blockedBy" />
          <span>
            {openBlockers.length === 1
              ? 'Blocked by 1 unfinished issue'
              : `Blocked by ${String(openBlockers.length)} unfinished issues`}
          </span>
        </p>
      )}

      {relations.length === 0 ? (
        <EmptyState
          icon={<RelationIcon />}
          title="No relations"
          description="Link this issue to what blocks it, duplicates it, or relates to it."
        />
      ) : (
        groups.map((group) => (
          <section key={group.type} className={styles.group}>
            <h3 className={styles.groupTitle}>{group.label}</h3>
            <ul className={styles.rows} aria-label={group.label}>
              {group.rows.map((relation) => (
                <li key={relation.id} className={styles.row}>
                  {/* `relationKindFrom` returns null for a type this build
                    * does not know, which a newer server could send. The row
                    * still renders -- the link is the useful part -- it just
                    * loses the glyph rather than crashing the panel. */}
                  {group.kind !== null && (
                    <RelationIndicator
                      kind={group.kind satisfies RelationKind}
                      target={relation.issue.title}
                    />
                  )}
                  <Link to={paths.issue(relation.issue.id)} className={styles.rowLink}>
                    {relation.issue.title}
                  </Link>
                  {relation.issue.completedAt !== null && (
                    <span className={styles.done}>Done</span>
                  )}
                  <IconButton
                    icon={<CloseIcon />}
                    aria-label={`Remove ${group.label.toLowerCase()} ${relation.issue.title}`}
                    disabled={isBusy}
                    onClick={() => {
                      remove(relation)
                    }}
                  />
                </li>
              ))}
            </ul>
          </section>
        ))
      )}

      {loadMoreErrorMessage !== null && (
        <p role="alert" className={styles.formError}>
          {loadMoreErrorMessage}
        </p>
      )}

      {hasNextPage && (
        <Button
          variant="secondary"
          size="sm"
          loading={isLoadingMore}
          onClick={loadMore}
          className={styles.loadMore}
        >
          Load more relations
        </Button>
      )}

      <IssuePicker
        workspaceSlug={workspaceSlug}
        open={isPicking}
        onClose={() => {
          setIsPicking(false)
        }}
        title="Link another issue"
        description="Choose how the two issues relate, then pick one."
        errorMessage={pickerError}
        isSubmitting={isBusy}
        // This issue and everything already linked to it. The server refuses a
        // self-relation and a duplicate edge anyway; not offering them is the
        // difference between a picker that cannot fail and one that explains
        // its failures.
        excludeIds={[issueId, ...relations.map((relation) => relation.issue.id)]}
        onPick={(issue) => {
          void createRelation(issue.id, type).then((outcome) => {
            if (outcome.status !== 'ok') {
              setPickerError(describeOutcome(outcome))
              return
            }

            setIsPicking(false)
            setPickerError(null)
            setStatus(`Linked ${issue.identifier}`)
          })
        }}
      >
        <div className={styles.field}>
          <label htmlFor="relation-type" className={styles.fieldLabel}>
            This issue is
          </label>
          <Select
            id="relation-type"
            value={type}
            size="sm"
            onChange={(event) => {
              // The `<select>` can only hold the four values rendered below it,
              // so this narrows what the DOM types as a bare string.
              setType(event.target.value as IssueRelationType)
            }}
          >
            {RELATION_ORDER.map((group) => (
              <option key={group.type} value={group.type}>
                {group.label}
              </option>
            ))}
          </Select>
        </div>
      </IssuePicker>
    </Panel>
  )
}
