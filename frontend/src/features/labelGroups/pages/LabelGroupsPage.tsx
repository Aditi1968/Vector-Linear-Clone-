import { useCallback, useState } from 'react'

import { PageContent, PageHeader } from '../../../app/layout'
import {
  Badge,
  Button,
  Dialog,
  EmptyState,
  ErrorState,
  LabelIcon,
  List,
  ListRow,
  ListRowMain,
  ListRowMeta,
  Skeleton,
  VisuallyHidden,
} from '../../../components'
import { useLabelGroupActions, useLabelGroups } from '../api'
import type {
  LabelGroup,
  LabelGroupDraft,
  LabelGroupOutcome,
  LabelGroupValidationError,
} from '../api'
import { LabelGroupForm } from '../components/LabelGroupForm'
import { LabelGroupPanel } from '../components/LabelGroupPanel'
import { exclusivityLabel, labelsByGroup, ungroupedLabels } from '../lib/labelGroups'
import styles from '../labelGroups.module.css'

const NO_ERRORS: readonly LabelGroupValidationError[] = []
const NO_MEMBERS: readonly never[] = []

/**
 * Label groups: labels administered together, and optionally made mutually
 * exclusive.
 *
 * ## What a group is for
 *
 * Exclusivity. A group whose `exclusive` is true says an issue may wear at
 * most one of its labels -- "Priority: low / medium / high" is a choice, not a
 * set. Migration 021 enforces that in PostgreSQL, through a generated column
 * carried down onto `issue_labels` and a partial unique index over it, so the
 * second attachment is refused by the server rather than by a service that
 * remembered to count first.
 *
 * Two consequences shape this screen, and both are the constraint talking:
 *
 *   - Turning exclusivity ON is REFUSED for a group whose labels already share
 *     an issue. The flip cascades onto every member label and then onto the
 *     index, and the violation fails the whole UPDATE. That refusal is the
 *     feature -- "you cannot make this group exclusive while issues break the
 *     rule it would impose" -- so it is shown as itself and not swallowed.
 *   - Deleting a group ungroups its labels rather than deleting them.
 *     `labels_group_fk` is ON DELETE RESTRICT precisely so that the choice
 *     cannot be made silently by a constraint; `LabelService` removes the
 *     membership first, in the same transaction.
 *
 * ## Why this is one screen and not a list plus a detail route
 *
 * `ROUTE_SEGMENTS` declares `label-groups` and no `label-groups/:id`, so there
 * is no per-group URL to navigate to. Selecting one changes what the panel
 * beside the list shows, and the rows are `<button>`s rather than links.
 *
 * ## The membership shown is bounded, and the screen says so
 *
 * `LabelGroup` publishes no member list; membership lives on the label, so
 * "what is in this group" is assembled from one page of 50 labels. The panel
 * qualifies an empty group when more labels are outstanding rather than
 * letting the gap read as a fact.
 */
export function LabelGroupsPage() {
  const { groups, labels, hasMoreLabels, isLoading, errorMessage, retry } =
    useLabelGroups()

  const actions = useLabelGroupActions()

  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [isComposerOpen, setIsComposerOpen] = useState(false)
  const [editing, setEditing] = useState<LabelGroup | null>(null)
  const [formErrors, setFormErrors] = useState<readonly LabelGroupValidationError[]>(
    NO_ERRORS,
  )
  const [formMessage, setFormMessage] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)

  // The first group until somebody chooses otherwise, so the panel has
  // something to show rather than an empty frame beside a full list.
  const selected = groups.find((group) => group.id === selectedId) ?? groups[0] ?? null

  const membersByGroup = labelsByGroup(labels)
  const available = ungroupedLabels(labels)

  const closeForms = useCallback(() => {
    setIsComposerOpen(false)
    setEditing(null)
    setFormErrors(NO_ERRORS)
    setFormMessage(null)
  }, [])

  const openComposer = useCallback(() => {
    setFormErrors(NO_ERRORS)
    setFormMessage(null)
    setEditing(null)
    setIsComposerOpen(true)
  }, [])

  const openEditor = useCallback(() => {
    if (selected === null) {
      return
    }

    setFormErrors(NO_ERRORS)
    setFormMessage(null)
    setIsComposerOpen(false)
    setEditing(selected)
  }, [selected])

  /** Apply one form result: close on success, show what came back otherwise. */
  const applyOutcome = useCallback(
    (outcome: Awaited<ReturnType<typeof actions.createGroup>>) => {
      if (outcome.status === 'ok') {
        closeForms()
        return
      }

      if (outcome.status === 'rejected') {
        setFormErrors(outcome.errors)
        setFormMessage(null)
        return
      }

      setFormErrors(NO_ERRORS)
      setFormMessage(outcome.message)
    },
    [closeForms],
  )

  const handleCreate = useCallback(
    (draft: LabelGroupDraft) => {
      void actions.createGroup(draft).then(applyOutcome)
    },
    [actions, applyOutcome],
  )

  const handleUpdate = useCallback(
    (draft: LabelGroupDraft) => {
      if (editing === null) {
        return
      }

      void actions.updateGroup(editing.id, draft).then(applyOutcome)
    },
    [actions, applyOutcome, editing],
  )

  /**
   * Report a write that was not made through a form.
   *
   * Above the list rather than beside the control: the row it refers to may
   * have moved by the time it is read, and a refusal anchored to a vanished
   * row is one nobody sees.
   */
  const report = useCallback(
    // Generic in what the write returned, because the three callers below
    // return three different things -- a group, a label, a deleted id -- and
    // none of them is what this function looks at.
    <TValue,>(outcome: LabelGroupOutcome<TValue>, fallback: string): boolean => {
      if (outcome.status === 'ok') {
        setActionError(null)
        return true
      }

      setActionError(
        outcome.status === 'failed'
          ? outcome.message
          : (outcome.errors[0]?.message ?? fallback),
      )

      return false
    },
    [],
  )

  const handleDelete = useCallback(() => {
    if (selected === null) {
      return
    }

    void actions.deleteGroup(selected.id).then((outcome) => {
      if (report(outcome, 'That group could not be deleted.')) {
        // Selecting nothing rather than the next row: which group follows a
        // deleted one is the refetched list's answer, and `selected` falls
        // back to the first on its own.
        setSelectedId(null)
      }
    })
  }, [actions, report, selected])

  const handleAddLabel = useCallback(
    (labelId: string) => {
      if (selected === null) {
        return
      }

      void actions.setLabelGroup(labelId, selected.id).then((outcome) => {
        report(outcome, 'That label could not be added to this group.')
      })
    },
    [actions, report, selected],
  )

  const handleRemoveLabel = useCallback(
    (labelId: string) => {
      void actions.setLabelGroup(labelId, null).then((outcome) => {
        report(outcome, 'That label could not be removed from this group.')
      })
    },
    [actions, report],
  )

  return (
    <>
      <PageHeader
        title="Label groups"
        description="Labels that are mutually exclusive, administered together."
        actions={
          <Button onClick={openComposer} variant="primary">
            New group
          </Button>
        }
      />

      <PageContent>
        {actionError !== null && (
          <p className={styles.actionError} role="alert">
            {actionError}
          </p>
        )}

        {isLoading && (
          <div className={styles.skeletonStack} role="status" aria-busy="true">
            <VisuallyHidden as="div">Loading label groups</VisuallyHidden>
            {Array.from({ length: 3 }, (_unused, index) => (
              <Skeleton key={index} width="100%" height="2.75rem" />
            ))}
          </div>
        )}

        {!isLoading && errorMessage !== null && (
          <ErrorState
            title="Could not load label groups"
            description={errorMessage}
            onRetry={retry}
          />
        )}

        {!isLoading && errorMessage === null && groups.length === 0 && (
          <EmptyState
            icon={<LabelIcon />}
            title="No label groups yet"
            description="A group gathers labels that answer one question — a priority, a platform, a customer tier. Make it exclusive and an issue may wear only one of them, enforced by the database rather than by anybody remembering."
            actions={
              <Button onClick={openComposer} variant="primary">
                Create the first group
              </Button>
            }
          />
        )}

        {!isLoading && errorMessage === null && groups.length > 0 && (
          <div className={styles.split}>
            <div className={styles.column}>
              <List label="Label groups">
                {groups.map((group) => {
                  const members = membersByGroup.get(group.id) ?? NO_MEMBERS

                  return (
                    <ListRow interactive key={group.id} selected={group.id === selected?.id}>
                      <ListRowMain>
                        <button
                          aria-current={group.id === selected?.id}
                          className={styles.rowButton}
                          onClick={() => {
                            setSelectedId(group.id)
                          }}
                          type="button"
                        >
                          <span className={styles.rowName}>{group.name}</span>
                          <span className={styles.rowMeta}>
                            <span>
                              {members.length}{' '}
                              {members.length === 1 ? 'label' : 'labels'}
                            </span>
                          </span>
                        </button>
                      </ListRowMain>

                      <ListRowMeta>
                        <Badge tone={group.exclusive ? 'warning' : 'neutral'}>
                          {exclusivityLabel(group)}
                        </Badge>
                      </ListRowMeta>
                    </ListRow>
                  )
                })}
              </List>

              {hasMoreLabels && (
                <p className={styles.hint}>
                  The counts are drawn from the first page of this workspace’s labels. A
                  group holding labels beyond it will read lower than it is.
                </p>
              )}
            </div>

            {selected !== null && (
              <div className={styles.column}>
                <LabelGroupPanel
                  available={available}
                  group={selected}
                  hasMoreLabels={hasMoreLabels}
                  isSaving={actions.isSaving}
                  members={membersByGroup.get(selected.id) ?? NO_MEMBERS}
                  onAddLabel={handleAddLabel}
                  onDelete={handleDelete}
                  onEdit={openEditor}
                  onRemoveLabel={handleRemoveLabel}
                />
              </div>
            )}
          </div>
        )}
      </PageContent>

      <Dialog
        open={isComposerOpen}
        onClose={closeForms}
        title="New label group"
        description="A name, and whether its labels exclude one another."
      >
        <LabelGroupForm
          errorMessage={formMessage}
          errors={formErrors}
          isSaving={actions.isSaving}
          onCancel={closeForms}
          onSubmit={handleCreate}
          submitLabel="Create group"
        />
      </Dialog>

      <Dialog
        open={editing !== null}
        onClose={closeForms}
        title="Edit label group"
        description={editing?.name}
      >
        {editing !== null && (
          <LabelGroupForm
            errorMessage={formMessage}
            errors={formErrors}
            initialExclusive={editing.exclusive}
            initialName={editing.name}
            isSaving={actions.isSaving}
            onCancel={closeForms}
            onSubmit={handleUpdate}
            submitLabel="Save changes"
          />
        )}
      </Dialog>
    </>
  )
}
