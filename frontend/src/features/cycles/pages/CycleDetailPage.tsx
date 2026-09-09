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
  Skeleton,
  Spinner,
  VisuallyHidden,
} from '../../../components'
import type { MenuItem } from '../../../components'
// The flat bar every progress reading outside a project's health dial uses.
// It lives under `features/projects` because that is where three of its four
// call sites are and because `components/` was not this change's to add to;
// the same cross-feature import the board already makes of `issues`.
// ponytail: promote to `components/` when someone owns that directory.
import { ProgressBar } from '../../projects/components/ProgressBar'
import {
  useCycleActions,
  useCycleDetail,
  useCycleIssues,
  useCycleUnscheduledIssues,
} from '../api'
import type { CycleDraft, CycleValidationError } from '../api'
import { CycleForm } from '../components/CycleForm'
import { cyclePhase, cycleTitle, formatCycleDate } from '../lib/cycles'
import styles from '../cycles.module.css'

const NO_ERRORS: readonly CycleValidationError[] = []

const PHASE_LABEL = {
  current: 'Current',
  upcoming: 'Upcoming',
  past: 'Past',
} as const

/**
 * One cycle: when it runs, where it sits relative to today, and what is in it.
 *
 * ## The server decides what is in it
 *
 * `filter: { cycleId }`, so the list below is the cycle's rather than the
 * workspace's sifted for it, and `totalCount` is how many issues the cycle
 * holds. Scoping to the cycle's team as well would be redundant -- a cycle
 * belongs to one team -- so `Cycle.teamId` is used for the one question that
 * does need it: which issues may be *added*, since `issueSetCycle` refuses a
 * cycle that is not the issue's team's.
 *
 * What survives is paging: while another page is outstanding the panel says
 * how many of the total are on screen, and the progress label says the count
 * is of what has been loaded.
 */
export function CycleDetailPage() {
  const params = useParams()
  const cycleId = params[CYCLE_ID_PARAM]
  const paths = useAppPaths()
  const navigate = useNavigate()

  const { cycle, isLoading, isNotFound, errorMessage, retry } = useCycleDetail(cycleId)
  const issueQuery = useCycleIssues(cycleId)
  const unscheduled = useCycleUnscheduledIssues(cycle?.teamId)
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
  const cycleIssues = issueQuery.issues
  const closed = cycleIssues.filter((issue) => issue.completedAt !== null).length

  const addItems: readonly MenuItem[] = unscheduled.map((issue) => ({
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
                <dd className={styles.factValue}>
                  <span className={styles.factFigure}>{cycle.number}</span>
                </dd>

                <dt className={styles.factTerm}>Starts</dt>
                <dd className={styles.factValue}>
                  <time className={styles.factFigure} dateTime={cycle.startsAt}>
                    {formatCycleDate(cycle.startsAt)}
                  </time>
                </dd>

                <dt className={styles.factTerm}>Ends</dt>
                <dd className={styles.factValue}>
                  <time className={styles.factFigure} dateTime={cycle.endsAt}>
                    {formatCycleDate(cycle.endsAt)}
                  </time>
                </dd>

                <dt className={styles.factTerm}>Progress</dt>
                <dd className={styles.factValue}>
                  <span className={styles.progressRow}>
                    {/* A flat bar, not a dial. The one ring in this product
                        is a project's health, where a single number stands
                        for a whole thing; a cycle's completion is a fraction
                        and reads as one. */}
                    <ProgressBar
                      value={closed}
                      total={cycleIssues.length}
                      label={
                        issueQuery.hasNextPage
                          ? `${title}: closed issues among those loaded`
                          : `${title}: closed issues`
                      }
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
                  label="Add an unscheduled issue to this cycle"
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
                      description="Nothing has been scheduled into it yet."
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
                            {/* One "Remove" per row, so the visible word is
                                not a name -- a screen-reader user asking for
                                the buttons on this screen would hear
                                "Remove, Remove, Remove" and have no way to
                                tell which issue each one drops. The label
                                names the issue; the visible text stays the
                                one word the column has room for. */}
                            <Button
                              size="sm"
                              variant="ghost"
                              aria-label={`Remove ${issue.identifier} from this cycle`}
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

                  {/* Only while the answer is partial: a count that matches
                      what is on screen is a sentence nobody needs to read. */}
                  {issueQuery.hasNextPage && (
                    <>
                      <p className={styles.footnote} role="status">
                        Showing {cycleIssues.length} of {issueQuery.totalCount}{' '}
                        issues in this cycle.
                      </p>

                      <Button
                        size="sm"
                        onClick={issueQuery.loadMore}
                        loading={issueQuery.isLoadingMore}
                      >
                        Load more
                      </Button>
                    </>
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
