import { useCallback, useMemo, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'

import { PageContent, PageHeader } from '../../../app/layout'
import { CYCLE_ID_PARAM, useAppPaths } from '../../../app/routes'
import {
  Badge,
  Button,
  Dialog,
  EmptyState,
  ErrorState,
  IssuesIcon,
  List,
  ListRow,
  ListRowMain,
  ListRowMeta,
  Menu,
  PlusIcon,
  ProgressIndicator,
  Skeleton,
  Spinner,
  VisuallyHidden,
} from '../../../components'
import type { MenuItem } from '../../../components'
import { useCycleActions, useCycleDetail, useCycleIssues } from '../api'
import type { CycleDraft, CycleValidationError } from '../api'
import { CycleForm } from '../components/CycleForm'
import { cyclePhase, cycleTitle, formatCycleDate } from '../lib/cycles'
import styles from '../cycles.module.css'

const NO_ERRORS: readonly CycleValidationError[] = []

/** How many unassigned issues the "add an issue" menu offers. See ADDABLE_LIMIT in projects. */
const ADDABLE_LIMIT = 15

const PHASE_LABEL = {
  current: 'Current',
  upcoming: 'Upcoming',
  past: 'Past',
} as const

/**
 * One cycle: when it runs, where it sits relative to today, and what is in it.
 *
 * ## The issue list is filtered in the browser, and says so
 *
 * There is no `cycle.issues` field and no `issues(cycleId:)` argument, so the
 * issues here are the *workspace's*, matched on `issue.cycle.id`. Worse than
 * for projects: `Cycle` exposes no `teamId` either, so the query cannot even
 * be scoped to the cycle's team -- a screen holding a cycle id has no way to
 * recover which team it belongs to.
 *
 * The footnote states exactly that, "Load more" says it loads more of the
 * workspace, and the progress indicator is labelled as counting loaded
 * issues. A panel that quietly showed a partial list as a complete one would
 * be the same UI with a lie in it.
 */
export function CycleDetailPage() {
  const params = useParams()
  const cycleId = params[CYCLE_ID_PARAM]
  const paths = useAppPaths()
  const navigate = useNavigate()

  const { cycle, isLoading, isNotFound, errorMessage, retry } = useCycleDetail(cycleId)
  const issueQuery = useCycleIssues()
  const actions = useCycleActions()

  const [isEditing, setIsEditing] = useState(false)
  const [formErrors, setFormErrors] = useState<readonly CycleValidationError[]>(NO_ERRORS)
  const [formMessage, setFormMessage] = useState<string | null>(null)
  const [panelMessage, setPanelMessage] = useState<string | null>(null)

  // One instant for the whole render, so the header's phase badge and any
  // future comparison below it cannot disagree.
  const now = useMemo(() => Date.now(), [])

  const openEditor = useCallback(() => {
    setFormErrors(NO_ERRORS)
    setFormMessage(null)
    setIsEditing(true)
  }, [])

  const closeEditor = useCallback(() => {
    setIsEditing(false)
  }, [])

  const handleUpdate = useCallback(
    (draft: CycleDraft) => {
      if (cycle === null) {
        return
      }

      void actions.updateCycle(cycle.id, draft).then((outcome) => {
        if (outcome.status === 'ok') {
          setIsEditing(false)
          return
        }

        if (outcome.status === 'rejected') {
          setFormErrors(outcome.errors)
          setFormMessage(null)
          return
        }

        setFormErrors(NO_ERRORS)
        setFormMessage(outcome.message)
      })
    },
    [actions, cycle],
  )

  const handleDelete = useCallback(() => {
    if (cycle === null) {
      return
    }

    void actions.deleteCycle(cycle.id).then((outcome) => {
      if (outcome.status === 'ok') {
        // `replace`, so Back does not return to a cycle that no longer
        // resolves.
        void navigate(paths.cycles(), { replace: true })
        return
      }

      setPanelMessage(
        outcome.status === 'failed'
          ? outcome.message
          : outcome.errors.map((error) => error.message).join(' '),
      )
    })
  }, [actions, cycle, navigate, paths])

  const handleSetCycle = useCallback(
    (issueId: string, targetCycleId: string | null) => {
      void actions.setIssueCycle(issueId, targetCycleId).then((outcome) => {
        setPanelMessage(
          outcome.status === 'ok'
            ? null
            : outcome.status === 'failed'
              ? outcome.message
              : outcome.errors.map((error) => error.message).join(' '),
        )
      })
    },
    [actions],
  )

  if (isLoading) {
    return (
      <>
        <PageHeader title="Cycle" />
        <PageContent>
          <div className={styles.skeletonStack} role="status" aria-busy="true">
            <VisuallyHidden as="div">Loading cycle</VisuallyHidden>
            <Skeleton width="14rem" height="1.5rem" />
            <Skeleton width="100%" height="8rem" />
          </div>
        </PageContent>
      </>
    )
  }

  if (errorMessage !== null) {
    return (
      <>
        <PageHeader title="Cycle" />
        <PageContent>
          <ErrorState
            title="Could not load this cycle"
            description={errorMessage}
            onRetry={retry}
          />
        </PageContent>
      </>
    )
  }

  if (isNotFound || cycle === null) {
    return (
      <>
        <PageHeader title="Cycle not found" />
        <PageContent>
          {/* One screen for "no such cycle" and for "a cycle in another
              workspace": the server gives one answer to both -- null -- and
              distinguishing them here would leak whether an id exists to
              someone who cannot see it. */}
          <ErrorState
            title="No such cycle"
            description="It may have been deleted, or it belongs to a workspace you cannot see."
            actions={<Link to={paths.cycles()}>Back to cycles</Link>}
          />
        </PageContent>
      </>
    )
  }

  const title = cycleTitle(cycle)
  const phase = cyclePhase(cycle, now)
  const cycleIssues = issueQuery.issues.filter((issue) => issue.cycle?.id === cycle.id)
  const closed = cycleIssues.filter((issue) => issue.completedAt !== null).length

  const addable = issueQuery.issues
    .filter((issue) => issue.cycle === null)
    .slice(0, ADDABLE_LIMIT)

  const addItems: readonly MenuItem[] = addable.map((issue) => ({
    id: issue.id,
    label: `${issue.identifier} ${issue.title}`,
    disabled: actions.isSaving,
    onSelect: () => {
      handleSetCycle(issue.id, cycle.id)
    },
  }))

  const menuItems: readonly MenuItem[] = [
    { id: 'edit', label: 'Edit cycle', disabled: actions.isSaving, onSelect: openEditor },
    {
      id: 'delete',
      label: 'Delete cycle',
      destructive: true,
      disabled: actions.isSaving,
      separatorBefore: true,
      onSelect: handleDelete,
    },
  ]

  return (
    <>
      <PageHeader
        title={title}
        actions={
          <>
            {/* The phase is a derived fact, not a stored one -- see
                ../lib/cycles.ts. Shown as a word rather than only as a
                colour, because a colour is not information. */}
            <Badge tone={phase === 'current' ? 'success' : 'neutral'}>
              {PHASE_LABEL[phase]}
            </Badge>
            <Menu label={`Actions for ${title}`} items={menuItems} align="end" />
          </>
        }
      />

      <PageContent>
        {panelMessage !== null && (
          <p className={styles.formError} role="alert">
            {panelMessage}
          </p>
        )}

        <div className={styles.stack}>
          <section className={styles.panel} aria-labelledby="cycle-overview">
            <div className={styles.panelHeader}>
              <h2 className={styles.panelTitle} id="cycle-overview">
                Overview
              </h2>
            </div>

            <div className={styles.panelBody}>
              <dl className={styles.facts}>
                <dt className={styles.factTerm}>Number</dt>
                <dd className={styles.factValue}>{cycle.number}</dd>

                <dt className={styles.factTerm}>Starts</dt>
                <dd className={styles.factValue}>
                  <time dateTime={cycle.startsAt}>{formatCycleDate(cycle.startsAt)}</time>
                </dd>

                <dt className={styles.factTerm}>Ends</dt>
                <dd className={styles.factValue}>
                  <time dateTime={cycle.endsAt}>{formatCycleDate(cycle.endsAt)}</time>
                </dd>

                <dt className={styles.factTerm}>Progress</dt>
                <dd className={styles.factValue}>
                  <span className={styles.progressRow}>
                    <ProgressIndicator
                      value={closed}
                      total={cycleIssues.length}
                      label={`${title}: closed issues among those loaded`}
                      showLabel
                    />
                    <span>closed &middot; completed or canceled</span>
                  </span>
                </dd>
              </dl>
            </div>
          </section>

          <section className={styles.panel} aria-labelledby="cycle-issues">
            <div className={styles.panelHeader}>
              <h2 className={styles.panelTitle} id="cycle-issues">
                Issues
              </h2>
              {addItems.length > 0 && (
                <Menu
                  label="Add a loaded issue to this cycle"
                  items={addItems}
                  icon={<PlusIcon />}
                  size="sm"
                  align="end"
                >
                  Add issue
                </Menu>
              )}
            </div>

            <div className={styles.panelBody}>
              {issueQuery.isLoading && <Spinner label="Loading issues" />}

              {issueQuery.errorMessage !== null && (
                <p className={styles.formError} role="alert">
                  {issueQuery.errorMessage}
                </p>
              )}

              {!issueQuery.isLoading && issueQuery.errorMessage === null && (
                <>
                  {cycleIssues.length === 0 ? (
                    <EmptyState
                      icon={<IssuesIcon />}
                      title="No issues in this cycle"
                      description={
                        issueQuery.hasNextPage
                          ? 'None among the issues loaded so far. Older issues may be in it -- load more below.'
                          : 'Every issue in this workspace has been checked; none is in this cycle.'
                      }
                    />
                  ) : (
                    <List label="Issues in this cycle">
                      {cycleIssues.map((issue) => (
                        <ListRow key={issue.id}>
                          <ListRowMain>
                            {issue.identifier} {issue.title}
                          </ListRowMain>
                          <ListRowMeta>
                            {issue.completedAt !== null && (
                              <Badge tone="success">Closed</Badge>
                            )}
                            <Button
                              size="sm"
                              variant="ghost"
                              disabled={actions.isSaving}
                              onClick={() => {
                                handleSetCycle(issue.id, null)
                              }}
                            >
                              Remove
                            </Button>
                          </ListRowMeta>
                        </ListRow>
                      ))}
                    </List>
                  )}

                  <p className={styles.footnote}>
                    The API offers no per-cycle issue filter -- and no way to
                    find a cycle&apos;s team from the cycle -- so this list is
                    matched in the browser against the {issueQuery.loadedCount}{' '}
                    most recent{' '}
                    {issueQuery.loadedCount === 1 ? 'issue' : 'issues'} in the
                    workspace. Loading more loads more of the workspace, not
                    more of this cycle.
                  </p>

                  {issueQuery.hasNextPage && (
                    <Button
                      size="sm"
                      onClick={issueQuery.loadMore}
                      loading={issueQuery.isLoadingMore}
                    >
                      Load more workspace issues
                    </Button>
                  )}
                </>
              )}
            </div>
          </section>
        </div>
      </PageContent>

      <Dialog open={isEditing} onClose={closeEditor} title="Edit cycle">
        <CycleForm
          initial={{
            number: cycle.number,
            name: cycle.name,
            startsAt: cycle.startsAt,
            endsAt: cycle.endsAt,
          }}
          submitLabel="Save changes"
          isSaving={actions.isSaving}
          onCancel={closeEditor}
          onSubmit={handleUpdate}
          errors={formErrors}
          errorMessage={formMessage}
        />
      </Dialog>
    </>
  )
}
