import { useCallback, useState } from 'react'
import type { CSSProperties } from 'react'

import { PageContent, PageHeader } from '../../../app/layout'
import {
  Badge,
  Button,
  Dialog,
  EmptyState,
  ErrorState,
  List,
  ListRow,
  ListRowMain,
  ListRowMeta,
  Menu,
  RelationIcon,
  Skeleton,
  VisuallyHidden,
} from '../../../components'
import { useWorkspaceContext } from '../../issues/api'
import { formatDay } from '../../projects/lib/projects'
import { useInitiativeActions, useInitiativeDetail, useInitiativeList } from '../api'
import type {
  Health,
  Initiative,
  InitiativeDraft,
  InitiativeValidationError,
} from '../api'
import { InitiativeDetailPanel } from '../components/InitiativeDetailPanel'
import { InitiativeForm } from '../components/InitiativeForm'
import {
  HEALTH_UNREPORTED,
  buildInitiativeTree,
  healthLabel,
  healthTone,
  initiativeStatusLabel,
  initiativeStatusTone,
} from '../lib/initiatives'
import styles from '../initiatives.module.css'

const NO_ERRORS: readonly InitiativeValidationError[] = []

/**
 * Initiatives: projects grouped into the larger thing they are part of.
 *
 * ## Why this is one screen and not a list plus a detail route
 *
 * `ROUTE_SEGMENTS` declares `initiatives` and no `initiatives/:id`, so there
 * is no per-initiative URL to navigate to. Selecting one therefore changes
 * what the panel beside the list shows rather than pushing history, and the
 * rows are `<button>`s rather than links -- an `<a>` with nowhere to point is
 * a link that breaks middle-click, "open in new tab" and the back button all
 * at once. A future `initiativeDetail` segment is the right way to make these
 * addressable; inventing one here would mean editing the shared path table.
 *
 * ## Why the list is a tree
 *
 * `initiativeSetParent` and `initiativeClearParent` exist, so nesting is a
 * real thing an initiative does and a flat list would make both mutations
 * invisible. The nesting is reconstructed from `parentInitiativeId` across
 * the rows in hand, because no query returns a subtree -- which means a child
 * whose parent is on a later page cannot be drawn under it. That row is drawn
 * at the top level and says so; see ../lib/initiatives.ts.
 *
 * ## Health is read-only here
 *
 * `Initiative.health` has no input field on either mutation. It is set by
 * posting an update -- a health and the sentence explaining it, together --
 * which the panel offers. A null health is "nobody has reported yet", which
 * is a different fact from "on track" and is shown as itself.
 */
export function InitiativesPage() {
  const { members, projects, isLoading: isLoadingContext } = useWorkspaceContext()

  const {
    initiatives,
    hasNextPage,
    isLoadingFirstPage,
    isLoadingMore,
    errorMessage,
    loadMoreErrorMessage,
    loadMore,
    retry,
  } = useInitiativeList()

  const actions = useInitiativeActions()

  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [editing, setEditing] = useState<Initiative | null>(null)
  const [isComposerOpen, setIsComposerOpen] = useState(false)
  const [formErrors, setFormErrors] = useState<readonly InitiativeValidationError[]>(
    NO_ERRORS,
  )
  const [formMessage, setFormMessage] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)

  // The first initiative until someone chooses otherwise, so the panel has
  // something to show rather than an empty frame beside a full list.
  const selected =
    initiatives.find((initiative) => initiative.id === selectedId) ??
    initiatives[0] ??
    null

  const detail = useInitiativeDetail(selected?.id ?? null)

  const rows = buildInitiativeTree(initiatives)

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

  const openEditor = useCallback((initiative: Initiative) => {
    setFormErrors(NO_ERRORS)
    setFormMessage(null)
    setIsComposerOpen(false)
    setEditing(initiative)
  }, [])

  /** Apply one form result: close on success, show what came back otherwise. */
  const applyOutcome = useCallback(
    (outcome: Awaited<ReturnType<typeof actions.createInitiative>>) => {
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

  /**
   * Report a write that was not made through a form.
   *
   * Above the list rather than beside the control: the row it refers to may
   * have moved by the time it is read, and a refusal anchored to a vanished
   * row is one nobody sees.
   */
  const reportOutcome = useCallback(
    (outcome: { status: string; errors?: readonly InitiativeValidationError[]; message?: string }, fallback: string) => {
      if (outcome.status === 'ok') {
        setActionError(null)
        return
      }

      setActionError(
        outcome.status === 'failed'
          ? (outcome.message ?? fallback)
          : (outcome.errors?.[0]?.message ?? fallback),
      )
    },
    [],
  )

  const handleCreate = useCallback(
    (draft: InitiativeDraft) => {
      void actions.createInitiative(draft).then(applyOutcome)
    },
    [actions, applyOutcome],
  )

  const handleUpdate = useCallback(
    (draft: InitiativeDraft) => {
      if (editing === null) {
        return
      }

      void actions.updateInitiative(editing.id, draft).then(applyOutcome)
    },
    [actions, applyOutcome, editing],
  )

  const handleDelete = useCallback(
    (initiative: Initiative) => {
      void actions.deleteInitiative(initiative.id).then((outcome) => {
        if (outcome.status === 'ok') {
          setActionError(null)

          // Selecting nothing rather than the next row: which initiative
          // follows a deleted one is the refetched list's answer, and
          // `selected` falls back to the first on its own.
          setSelectedId(null)
          return
        }

        reportOutcome(outcome, 'That initiative could not be deleted.')
      })
    },
    [actions, reportOutcome],
  )

  const handleAddProject = useCallback(
    (projectId: string) => {
      if (selected === null) {
        return
      }

      void actions.addProject(selected.id, projectId).then((outcome) => {
        reportOutcome(outcome, 'That project could not be added.')
      })
    },
    [actions, reportOutcome, selected],
  )

  const handleRemoveProject = useCallback(
    (projectId: string) => {
      if (selected === null) {
        return
      }

      void actions.removeProject(selected.id, projectId).then((outcome) => {
        reportOutcome(outcome, 'That project could not be removed.')
      })
    },
    [actions, reportOutcome, selected],
  )

  const handleSetParent = useCallback(
    (parentInitiativeId: string) => {
      if (selected === null) {
        return
      }

      void actions.setParent(selected.id, parentInitiativeId).then((outcome) => {
        reportOutcome(outcome, 'That initiative could not be nested.')
      })
    },
    [actions, reportOutcome, selected],
  )

  const handleClearParent = useCallback(() => {
    if (selected === null) {
      return
    }

    void actions.clearParent(selected.id).then((outcome) => {
      reportOutcome(outcome, 'That initiative could not be moved.')
    })
  }, [actions, reportOutcome, selected])

  const handlePostUpdate = useCallback(
    (health: Health, body: string) => {
      if (selected === null) {
        return
      }

      void actions.postUpdate(selected.id, health, body).then((outcome) => {
        reportOutcome(outcome, 'That update could not be posted.')
      })
    },
    [actions, reportOutcome, selected],
  )

  const isBusy = isLoadingFirstPage || isLoadingContext

  return (
    <>
      <PageHeader
        title="Initiatives"
        description="Projects grouped into the larger thing they are part of."
        actions={
          <Button onClick={openComposer} variant="primary">
            New initiative
          </Button>
        }
      />

      <PageContent>
        {actionError !== null && (
          <p className={styles.actionError} role="alert">
            {actionError}
          </p>
        )}

        {isBusy && (
          <div className={styles.skeletonStack} role="status" aria-busy="true">
            <VisuallyHidden as="div">Loading initiatives</VisuallyHidden>
            {Array.from({ length: 4 }, (_unused, index) => (
              <Skeleton key={index} width="100%" height="2.75rem" />
            ))}
          </div>
        )}

        {!isBusy && errorMessage !== null && (
          <ErrorState
            title="Could not load initiatives"
            description={errorMessage}
            onRetry={retry}
          />
        )}

        {!isBusy && errorMessage === null && initiatives.length === 0 && (
          <EmptyState
            icon={<RelationIcon />}
            title="No initiatives yet"
            description="An initiative is the thing several projects add up to — a launch, a migration, a quarter's theme. Projects join it; it can sit inside a larger one."
            actions={
              <Button onClick={openComposer} variant="primary">
                Create the first initiative
              </Button>
            }
          />
        )}

        {!isBusy && errorMessage === null && initiatives.length > 0 && (
          <div className={styles.split}>
            <div className={styles.column}>
              <List label="Initiatives">
                {rows.map(({ initiative, depth, parentNotShown }) => (
                  <ListRow
                    interactive
                    key={initiative.id}
                    selected={initiative.id === selected?.id}
                  >
                    <ListRowMain>
                      <button
                        aria-current={initiative.id === selected?.id}
                        className={styles.rowButton}
                        onClick={() => {
                          setSelectedId(initiative.id)
                        }}
                        /* Depth travels as a custom property so the
                           indentation step stays a token in the
                           stylesheet rather than a number in the JSX. */
                        style={{ '--initiative-depth': depth } as CSSProperties}
                        type="button"
                      >
                        <span className={styles.rowName}>{initiative.name}</span>
                        <span className={styles.rowMeta}>
                          <span>
                            {initiative.projectIds.length}{' '}
                            {initiative.projectIds.length === 1 ? 'project' : 'projects'}
                          </span>
                          {initiative.childInitiativeIds.length > 0 && (
                            <span>
                              {initiative.childInitiativeIds.length} nested
                            </span>
                          )}
                          {initiative.targetDate !== null && (
                            <time dateTime={initiative.targetDate}>
                              {formatDay(initiative.targetDate)}
                            </time>
                          )}
                          {parentNotShown && (
                            <span className={styles.unresolved}>
                              Nested under an initiative not shown here
                            </span>
                          )}
                        </span>
                      </button>
                    </ListRowMain>

                    <ListRowMeta>
                      <span className={styles.rowActions}>
                        <Badge tone={initiativeStatusTone(initiative.status)}>
                          {initiativeStatusLabel(initiative.status)}
                        </Badge>
                        {initiative.health === null ? (
                          <Badge tone="neutral">{HEALTH_UNREPORTED}</Badge>
                        ) : (
                          <Badge tone={healthTone(initiative.health)}>
                            {healthLabel(initiative.health)}
                          </Badge>
                        )}
                        <Menu
                          align="end"
                          items={[
                            {
                              id: 'edit',
                              label: 'Edit initiative...',
                              onSelect: () => {
                                openEditor(initiative)
                              },
                            },
                            {
                              id: 'delete',
                              label: 'Delete initiative',
                              destructive: true,
                              separatorBefore: true,
                              onSelect: () => {
                                handleDelete(initiative)
                              },
                            },
                          ]}
                          label={`Actions on ${initiative.name}`}
                          size="sm"
                        />
                      </span>
                    </ListRowMeta>
                  </ListRow>
                ))}
              </List>

              {loadMoreErrorMessage !== null && (
                <p className={styles.actionError} role="alert">
                  {loadMoreErrorMessage}
                </p>
              )}

              {hasNextPage && (
                <div className={styles.loadMore}>
                  {/* Said plainly: there is no count field on this
                      connection, so "of N" is a number nobody can supply. */}
                  <p className={styles.countLine}>
                    Showing <span className={styles.count}>{initiatives.length}</span>{' '}
                    initiatives. There are more, and a nested one may be among them.
                  </p>
                  <Button disabled={isLoadingMore} onClick={loadMore} variant="secondary">
                    {isLoadingMore ? 'Loading…' : 'Load more'}
                  </Button>
                </div>
              )}
            </div>

            {selected !== null && (
              <div className={styles.column}>
                <InitiativeDetailPanel
                  detail={detail.initiative}
                  errorMessage={detail.errorMessage}
                  initiatives={initiatives}
                  isLoading={detail.isLoading}
                  isNotFound={detail.isNotFound}
                  isSaving={actions.isSaving}
                  members={members}
                  onAddProject={handleAddProject}
                  onClearParent={handleClearParent}
                  onPostUpdate={handlePostUpdate}
                  onRemoveProject={handleRemoveProject}
                  onRetry={detail.retry}
                  onSetParent={handleSetParent}
                  projects={projects}
                  summary={selected}
                />
              </div>
            )}
          </div>
        )}
      </PageContent>

      <Dialog
        open={isComposerOpen}
        onClose={closeForms}
        title="New initiative"
        description="A name, and the shape of the thing the projects add up to."
      >
        <InitiativeForm
          errorMessage={formMessage}
          errors={formErrors}
          isSaving={actions.isSaving}
          members={members}
          onCancel={closeForms}
          onSubmit={handleCreate}
          submitLabel="Create initiative"
        />
      </Dialog>

      <Dialog
        open={editing !== null}
        onClose={closeForms}
        title="Edit initiative"
        description={editing?.name}
      >
        {editing !== null && (
          <InitiativeForm
            errorMessage={formMessage}
            errors={formErrors}
            initialDescription={editing.description}
            initialName={editing.name}
            initialOwnerId={editing.ownerId}
            initialStatus={editing.status}
            initialTargetDate={editing.targetDate}
            isSaving={actions.isSaving}
            members={members}
            onCancel={closeForms}
            onSubmit={handleUpdate}
            submitLabel="Save changes"
          />
        )}
      </Dialog>
    </>
  )
}
