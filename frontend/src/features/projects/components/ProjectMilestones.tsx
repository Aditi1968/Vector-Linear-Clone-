import { useCallback, useId, useState } from 'react'

import {
  Button,
  EmptyState,
  Input,
  Menu,
  ProgressIndicator,
} from '../../../components'
import type { MenuItem } from '../../../components'
import type { MilestoneDraft, ProjectIssue, ProjectMilestone } from '../api'
import { closedCount, formatDay, issuesInMilestone } from '../lib/projects'
import styles from '../projects.module.css'

interface MilestoneFormProps {
  initial?: MilestoneDraft
  submitLabel: string
  isSaving: boolean
  onCancel: () => void
  onSubmit: (draft: MilestoneDraft) => void
}

/**
 * Create or rename one milestone.
 *
 * Two fields, so it is inline rather than in a dialog: a modal for a name and
 * a date interrupts more than it helps, and it would put the milestone list
 * behind a scrim at the moment the user is comparing against it.
 *
 * `<input type="date">` rather than a picker component -- localised,
 * keyboard-navigable and screen-reader-labelled by the platform, and its
 * value format is already the `Date` scalar's `YYYY-MM-DD`.
 */
function MilestoneForm({
  initial,
  submitLabel,
  isSaving,
  onCancel,
  onSubmit,
}: MilestoneFormProps) {
  const [name, setName] = useState(initial?.name ?? '')
  const [targetDate, setTargetDate] = useState(initial?.targetDate ?? '')
  const fieldId = useId()

  return (
    <form
      className={styles.form}
      noValidate
      onSubmit={(event) => {
        event.preventDefault()

        const trimmed = name.trim()

        if (trimmed.length === 0) {
          return
        }

        onSubmit({ name: trimmed, targetDate: targetDate === '' ? null : targetDate })
      }}
    >
      <div className={styles.fieldRow}>
        <div className={styles.field}>
          <label className={styles.label} htmlFor={`${fieldId}-name`}>
            Milestone name
          </label>
          <Input
            id={`${fieldId}-name`}
            size="sm"
            value={name}
            autoFocus
            onChange={(event) => {
              setName(event.target.value)
            }}
          />
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={`${fieldId}-date`}>
            Target date
          </label>
          <Input
            id={`${fieldId}-date`}
            size="sm"
            type="date"
            value={targetDate}
            onChange={(event) => {
              setTargetDate(event.target.value)
            }}
          />
        </div>
      </div>

      <div className={styles.formActions}>
        <Button type="button" size="sm" variant="secondary" onClick={onCancel}>
          Cancel
        </Button>
        <Button
          type="submit"
          size="sm"
          variant="primary"
          loading={isSaving}
          disabled={name.trim().length === 0}
        >
          {submitLabel}
        </Button>
      </div>
    </form>
  )
}

export interface ProjectMilestonesProps {
  milestones: readonly ProjectMilestone[]
  /** The project's issues, already filtered. Milestone progress is derived from them. */
  issues: readonly ProjectIssue[]
  /** True while the whole issue set is still arriving, so progress is not yet meaningful. */
  isCounting: boolean
  isSaving: boolean
  onCreate: (draft: MilestoneDraft) => void
  onUpdate: (id: string, draft: MilestoneDraft) => void
  onDelete: (id: string) => void
}

/**
 * A project's milestones, with how far each has got.
 *
 * ## Where the progress comes from
 *
 * `ProjectMilestone` exposes no counts -- no `issueCount`, no
 * `completedCount`, nothing. The only route to a milestone's progress is the
 * issues that carry its `milestoneId`, which means it can only be computed
 * from the issues this screen has loaded. That is stated on the screen rather
 * than hidden: a bar claiming "3/4" that silently means "3 of the 4 I happen
 * to have" is worse than no bar.
 *
 * `completedAt` is non-null for canceled issues as well as completed ones
 * (the schema says so explicitly), so the count is of *closed* issues and is
 * labelled that way. Calling a canceled issue done would overstate progress,
 * which is the one direction a progress bar must never be wrong in.
 */
export function ProjectMilestones({
  milestones,
  issues,
  isCounting,
  isSaving,
  onCreate,
  onUpdate,
  onDelete,
}: ProjectMilestonesProps) {
  const [isCreating, setIsCreating] = useState(false)
  const [editingId, setEditingId] = useState<string | null>(null)

  const startCreate = useCallback(() => {
    setEditingId(null)
    setIsCreating(true)
  }, [])

  const stopEditing = useCallback(() => {
    setIsCreating(false)
    setEditingId(null)
  }, [])

  const handleCreate = useCallback(
    (draft: MilestoneDraft) => {
      setIsCreating(false)
      onCreate(draft)
    },
    [onCreate],
  )

  return (
    <section className={styles.panel} aria-labelledby="project-milestones">
      <div className={styles.panelHeader}>
        <h2 className={styles.panelTitle} id="project-milestones">
          Milestones
        </h2>
        {!isCreating && (
          <Button size="sm" onClick={startCreate}>
            Add milestone
          </Button>
        )}
      </div>

      <div className={styles.panelBody}>
        {isCreating && (
          <MilestoneForm
            submitLabel="Add milestone"
            isSaving={isSaving}
            onCancel={stopEditing}
            onSubmit={handleCreate}
          />
        )}

        {milestones.length === 0 && !isCreating && (
          <EmptyState
            title="No milestones"
            description="Milestones break a project into checkpoints. Issues can be filed against one."
          />
        )}

        {milestones.map((milestone) => {
          const inMilestone = issuesInMilestone(issues, milestone.id)
          const closed = closedCount(inMilestone)

          const items: readonly MenuItem[] = [
            {
              id: 'edit',
              label: 'Rename',
              disabled: isSaving,
              onSelect: () => {
                setIsCreating(false)
                setEditingId(milestone.id)
              },
            },
            {
              id: 'delete',
              label: 'Delete milestone',
              destructive: true,
              disabled: isSaving,
              separatorBefore: true,
              onSelect: () => {
                onDelete(milestone.id)
              },
            },
          ]

          return (
            <div className={styles.milestone} key={milestone.id}>
              <div className={styles.milestoneHead}>
                <h3 className={styles.milestoneName}>{milestone.name}</h3>
                <Menu
                  label={`Actions for ${milestone.name}`}
                  items={items}
                  size="sm"
                  align="end"
                />
              </div>

              {editingId === milestone.id ? (
                <MilestoneForm
                  initial={{ name: milestone.name, targetDate: milestone.targetDate }}
                  submitLabel="Save"
                  isSaving={isSaving}
                  onCancel={stopEditing}
                  onSubmit={(draft) => {
                    setEditingId(null)
                    onUpdate(milestone.id, draft)
                  }}
                />
              ) : (
                <div className={styles.milestoneMeta}>
                  {milestone.targetDate !== null && (
                    <span>
                      Target{' '}
                      <time dateTime={milestone.targetDate}>
                        {formatDay(milestone.targetDate)}
                      </time>
                    </span>
                  )}

                  {/* Suppressed rather than shown as 0/0 while the issue
                      query is still answering: an empty bar during loading
                      reads as "nothing done", which is a claim, not a
                      placeholder. */}
                  {isCounting ? (
                    <span>Counting issues...</span>
                  ) : (
                    <>
                      <ProgressIndicator
                        value={closed}
                        total={inMilestone.length}
                        label={`${milestone.name}: closed issues among those loaded`}
                        showLabel
                      />
                      <span>
                        {inMilestone.length === 1 ? 'issue' : 'issues'} closed, of those
                        loaded
                      </span>
                    </>
                  )}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </section>
  )
}
