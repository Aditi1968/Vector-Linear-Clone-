import { useCallback, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import { PageContent, PageHeader } from '../../../app/layout'
import { useAppPaths } from '../../../app/routes'
import {
  Badge,
  Button,
  Dialog,
  EmptyState,
  ErrorState,
  InspectorPanel,
  IssueRow,
  List,
  ListRow,
  ListRowMain,
  ListRowMeta,
  Menu,
  SearchIcon,
  Skeleton,
  VisuallyHidden,
  statusCategoryFrom,
} from '../../../components'
import { memberLabel, useWorkspaceContext } from '../../issues/api'
import { formatDueDate } from '../../issues/lib/dates'
import { describePriority, priorityLevel } from '../../issues/lib/priority'
import {
  useSavedViewActions,
  useSavedViewList,
  useSavedViewResults,
} from '../api'
import type { SavedViewDraft, SavedViewFields, SavedViewValidationError } from '../api'
import { SavedViewForm } from '../components/SavedViewForm'
import {
  countFilters,
  groupingLabel,
  layoutLabel,
  visibilityLabel,
} from '../lib/savedViews'
import styles from '../savedViews.module.css'

const NO_ERRORS: readonly SavedViewValidationError[] = []

/**
 * Saved views: the filter sets someone named and kept.
 *
 * ## Why this is one screen and not a list plus a detail route
 *
 * `ROUTE_SEGMENTS` declares `saved-views` and no `saved-views/:id`, so there
 * is no per-view URL to navigate to. Selecting a view therefore changes what
 * the panel beside the list shows rather than pushing history, and the rows
 * are `<button>`s rather than links -- an `<a>` with nowhere to point is a
 * link that breaks middle-click, "open in new tab" and the back button all at
 * once. A future `savedViewDetail` segment is the right way to make these
 * addressable; inventing one here would mean editing the shared path table.
 *
 * ## What a view's results are
 *
 * `SavedView.issues` -- the stored filter and ordering applied by the server.
 * There is no way to ask "what would this filter select" for a filter that
 * has not been saved, so this screen previews nothing: it shows what a saved
 * view selects, which is the only question the schema answers.
 *
 * Unlike a triage row, these nodes are full `Issue` objects, so the rows can
 * show a status glyph and an assignee -- both resolved through
 * `useWorkspaceContext()`'s lookups, which is one document for the screen
 * rather than one per row.
 */
export function SavedViewsPage() {
  const paths = useAppPaths()
  const { teams, stateById, memberById, isLoading: isLoadingContext } =
    useWorkspaceContext()

  const { views, hasNextPage, isLoading, errorMessage, retry } = useSavedViewList()
  const actions = useSavedViewActions()

  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [editing, setEditing] = useState<SavedViewFields | null>(null)
  const [isComposerOpen, setIsComposerOpen] = useState(false)
  const [formErrors, setFormErrors] = useState<readonly SavedViewValidationError[]>(NO_ERRORS)
  const [formMessage, setFormMessage] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)

  // The first view until someone chooses otherwise, so the panel has
  // something to show rather than an empty frame beside a full list.
  const selected = views.find((view) => view.id === selectedId) ?? views[0] ?? null

  const results = useSavedViewResults(selected?.id ?? null)

  const teamById = useMemo(
    () => new Map(teams.map((team) => [team.id, team])),
    [teams],
  )

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

  const openEditor = useCallback((view: SavedViewFields) => {
    setFormErrors(NO_ERRORS)
    setFormMessage(null)
    setIsComposerOpen(false)
    setEditing(view)
  }, [])

  /** Apply one form result: close on success, show what came back otherwise. */
  const applyOutcome = useCallback(
    (outcome: Awaited<ReturnType<typeof actions.createView>>) => {
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
    (draft: SavedViewDraft) => {
      void actions.createView(draft).then(applyOutcome)
    },
    [actions, applyOutcome],
  )

  const handleUpdate = useCallback(
    (draft: SavedViewDraft) => {
      if (editing === null) {
        return
      }

      void actions.updateView(editing.id, draft).then(applyOutcome)
    },
    [actions, applyOutcome, editing],
  )

  const handleDelete = useCallback(
    (view: SavedViewFields) => {
      void actions.deleteView(view.id).then((outcome) => {
        if (outcome.status === 'ok') {
          setActionError(null)

          // Selecting nothing rather than the next row: which view follows a
          // deleted one is the refetched list's answer, and `selected` falls
          // back to the first view on its own.
          setSelectedId(null)
          return
        }

        setActionError(
          outcome.status === 'failed'
            ? outcome.message
            : (outcome.errors[0]?.message ?? 'That view could not be deleted.'),
        )
      })
    },
    [actions],
  )

  const isBusy = isLoading || isLoadingContext

  return (
    <>
      <PageHeader
        title="Saved views"
        description="Filter sets someone named and kept."
        actions={
          <Button onClick={openComposer} variant="primary">
            New view
          </Button>
        }
      />

      <PageContent>
        {actionError !== null && (
          <p className={styles.formError} role="alert">
            {actionError}
          </p>
        )}

        {isBusy && (
          <div className={styles.skeletonStack} role="status" aria-busy="true">
            <VisuallyHidden as="div">Loading saved views</VisuallyHidden>
            {Array.from({ length: 4 }, (_unused, index) => (
              <Skeleton key={index} width="100%" height="2.75rem" />
            ))}
          </div>
        )}

        {!isBusy && errorMessage !== null && (
          <ErrorState
            title="Could not load saved views"
            description={errorMessage}
            onRetry={retry}
          />
        )}

        {!isBusy && errorMessage === null && views.length === 0 && (
          <EmptyState
            icon={<SearchIcon />}
            title="No saved views yet"
            description="A saved view keeps a filter, an ordering and a layout under a name, so a question you ask often is one click away."
            actions={
              <Button onClick={openComposer} variant="primary">
                Create the first view
              </Button>
            }
          />
        )}

        {!isBusy && errorMessage === null && views.length > 0 && (
          <div className={styles.split}>
            <div className={styles.column}>
              <List label="Saved views">
                {views.map((view) => {
                  const team = view.teamId === null ? undefined : teamById.get(view.teamId)
                  const filters = countFilters(view.filter)

                  return (
                    <ListRow interactive key={view.id} selected={view.id === selected?.id}>
                      <ListRowMain>
                        <button
                          aria-current={view.id === selected?.id}
                          className={styles.viewButton}
                          onClick={() => {
                            setSelectedId(view.id)
                          }}
                          type="button"
                        >
                          <span className={styles.viewName}>{view.name}</span>
                          <span className={styles.viewMeta}>
                            <span>{team === undefined ? 'Whole workspace' : team.key}</span>
                            <span>{layoutLabel(view.layout)}</span>
                            {view.grouping !== null && (
                              <span>Grouped by {groupingLabel(view.grouping)}</span>
                            )}
                            <span>
                              {filters === 0
                                ? 'No filters'
                                : `${String(filters)} ${filters === 1 ? 'filter' : 'filters'}`}
                            </span>
                          </span>
                        </button>
                      </ListRowMain>

                      <ListRowMeta>
                        <span className={styles.rowActions}>
                          <Badge
                            tone={view.visibility === 'SHARED' ? 'accent' : 'neutral'}
                          >
                            {visibilityLabel(view.visibility)}
                          </Badge>
                          <Menu
                            align="end"
                            items={[
                              {
                                id: 'edit',
                                label: 'Edit view...',
                                onSelect: () => {
                                  openEditor(view)
                                },
                              },
                              {
                                id: 'delete',
                                label: 'Delete view',
                                destructive: true,
                                separatorBefore: true,
                                onSelect: () => {
                                  handleDelete(view)
                                },
                              },
                            ]}
                            label={`Actions on ${view.name}`}
                            size="sm"
                          />
                        </span>
                      </ListRowMeta>
                    </ListRow>
                  )
                })}
              </List>

              {/* The list is one page. Said plainly rather than left to be
                  discovered by a view that is simply missing. */}
              {hasNextPage && (
                <p className={styles.countLine}>
                  Showing the first <span className={styles.count}>{views.length}</span>{' '}
                  views. This workspace has more than one page of them.
                </p>
              )}
            </div>

            {selected !== null && (
              <div className={styles.column}>
                <InspectorPanel
                  label={`Issues in ${selected.name}`}
                  header={
                    <div className={styles.panelHeader}>
                      <h2 className={styles.panelTitle}>{selected.name}</h2>
                      {!results.isLoading && results.errorMessage === null && (
                        <span className={styles.panelCount}>
                          {results.totalCount}{' '}
                          {results.totalCount === 1 ? 'issue' : 'issues'}
                        </span>
                      )}
                    </div>
                  }
                >
                  {results.isLoading && (
                    <div className={styles.skeletonStack} role="status" aria-busy="true">
                      <VisuallyHidden as="div">Loading the view's issues</VisuallyHidden>
                      {Array.from({ length: 4 }, (_unused, index) => (
                        <Skeleton key={index} width="100%" height="2.25rem" />
                      ))}
                    </div>
                  )}

                  {!results.isLoading && results.errorMessage !== null && (
                    <ErrorState
                      title="Could not load this view's issues"
                      description={results.errorMessage}
                      onRetry={results.retry}
                    />
                  )}

                  {/* Null is "no such view", which the server answers
                      identically for one that was deleted and one the viewer
                      may not see -- on purpose. */}
                  {!results.isLoading &&
                    results.errorMessage === null &&
                    results.isNotFound && (
                      <EmptyState
                        title="This view is no longer available"
                        description="It may have been deleted. Refresh the list to see what is left."
                      />
                    )}

                  {!results.isLoading &&
                    results.errorMessage === null &&
                    !results.isNotFound &&
                    results.issues.length === 0 && (
                      <EmptyState
                        title="Nothing matches this view"
                        description="The filter is valid — no issue in the workspace satisfies it right now."
                      />
                    )}

                  {!results.isLoading &&
                    results.errorMessage === null &&
                    results.issues.length > 0 && (
                      <>
                        <List label={`Issues in ${selected.name}`}>
                          {results.issues.map((issue) => {
                            const state = stateById.get(issue.workflowStateId)
                            const assignee =
                              issue.assigneeId === null
                                ? undefined
                                : memberById.get(issue.assigneeId)
                            const priority = describePriority(issue.priority)

                            return (
                              <IssueRow
                                identifier={issue.identifier}
                                key={issue.id}
                                priority={priorityLevel(issue.priority)}
                                priorityName={priority.name ?? undefined}
                                status={
                                  state === undefined
                                    ? undefined
                                    : (statusCategoryFrom(state.category) ?? undefined)
                                }
                                statusName={state?.name}
                                title={
                                  <Link
                                    className={styles.resultLink}
                                    to={paths.issue(issue.id)}
                                  >
                                    {issue.title}
                                  </Link>
                                }
                                meta={
                                  <>
                                    {issue.dueDate !== null && (
                                      <time className={styles.due} dateTime={issue.dueDate}>
                                        {formatDueDate(issue.dueDate)}
                                      </time>
                                    )}
                                    {assignee !== undefined && (
                                      <span>{memberLabel(assignee)}</span>
                                    )}
                                  </>
                                }
                              />
                            )
                          })}
                        </List>

                        {results.hasNextPage && (
                          <p className={styles.countLine}>
                            Showing{' '}
                            <span className={styles.count}>{results.issues.length}</span>{' '}
                            of <span className={styles.count}>{results.totalCount}</span>.
                          </p>
                        )}
                      </>
                    )}
                </InspectorPanel>
              </div>
            )}
          </div>
        )}
      </PageContent>

      <Dialog
        open={isComposerOpen}
        onClose={closeForms}
        title="New saved view"
        description="A name, a filter and an ordering, kept together."
      >
        <SavedViewForm
          errorMessage={formMessage}
          errors={formErrors}
          isSaving={actions.isSaving}
          onCancel={closeForms}
          onSubmit={handleCreate}
          submitLabel="Create view"
          teams={teams}
        />
      </Dialog>

      <Dialog
        open={editing !== null}
        onClose={closeForms}
        title="Edit saved view"
        description={editing?.name}
      >
        {editing !== null && (
          <SavedViewForm
            errorMessage={formMessage}
            errors={formErrors}
            initialFilter={editing.filter}
            initialGrouping={editing.grouping}
            initialLayout={editing.layout}
            initialName={editing.name}
            initialOrderDirection={editing.orderDirection}
            initialOrderField={editing.orderField}
            initialTeamId={editing.teamId}
            initialVisibility={editing.visibility}
            isSaving={actions.isSaving}
            onCancel={closeForms}
            onSubmit={handleUpdate}
            submitLabel="Save changes"
            teams={teams}
          />
        )}
      </Dialog>
    </>
  )
}
