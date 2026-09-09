import { useCallback, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import { PageContent, PageHeader } from '../../../app/layout'
import { useAppPaths } from '../../../app/routes'
import {
  Button,
  CycleIcon,
  Dialog,
  EmptyState,
  ErrorState,
  List,
  ListRow,
  ListRowMain,
  ListRowMeta,
  Select,
  Skeleton,
  VisuallyHidden,
} from '../../../components'
import { useCycleActions, useCycleList, useCycleTeams } from '../api'
import type { CycleDraft, CycleFields, CycleValidationError } from '../api'
import { CycleForm } from '../components/CycleForm'
import { cycleTitle, formatCycleRange, groupByPhase } from '../lib/cycles'
import styles from '../cycles.module.css'

const NO_ERRORS: readonly CycleValidationError[] = []

/** The three phases, in the order they are read in: now, next, done. */
const PHASES = [
  {
    key: 'current',
    title: 'Current',
    empty: 'No cycle covers today.',
  },
  {
    key: 'upcoming',
    title: 'Upcoming',
    empty: 'Nothing is scheduled after today.',
  },
  {
    key: 'past',
    title: 'Past',
    empty: 'No cycle has finished yet.',
  },
] as const

/**
 * A team's cycles, split into current, upcoming and past.
 *
 * ## Why there is a team picker and not a workspace-wide list
 *
 * `cycles(workspaceSlug:, teamId:)` requires a team. That is not an API
 * inconvenience: a cycle *belongs* to a team -- it is numbered within one and
 * `cycles_no_overlap` is enforced per team -- so "the workspace's cycles" is
 * not a thing that exists. The picker is the schema showing through, and it
 * is the first control on the screen for that reason.
 *
 * The team is component state rather than a URL segment. `useAppPaths()` has
 * one builder for this screen, deliberately: `/acme/cycles` has to resolve
 * for anyone who follows it, and a slug plus a team UUID is not a URL a
 * person can type or share meaningfully.
 *
 * ## Why the phases are derived here
 *
 * The backend does not label them -- no `current` field, no phase enum, no
 * "active cycle" query. It returns `startsAt` and `endsAt` and leaves the
 * comparison to the caller. ../lib/cycles.ts makes it, against the same
 * half-open interval the database uses.
 */
export function CycleListPage() {
  const paths = useAppPaths()

  const { teams, isLoading: isLoadingTeams, errorMessage: teamsError } = useCycleTeams()
  const [selectedTeamId, setSelectedTeamId] = useState<string | null>(null)

  // The first team until someone chooses otherwise. A default and not a
  // decision: the screen needs *a* team to ask about, and the picker beside
  // it is how the choice is made and how it is visible.
  const teamId = selectedTeamId ?? teams[0]?.id
  const team = teams.find((candidate) => candidate.id === teamId)

  const { cycles, isLoading, errorMessage, retry } = useCycleList(teamId)
  const actions = useCycleActions()

  const [isComposerOpen, setIsComposerOpen] = useState(false)
  const [formErrors, setFormErrors] = useState<readonly CycleValidationError[]>(NO_ERRORS)
  const [formMessage, setFormMessage] = useState<string | null>(null)

  /**
   * One instant for the whole render.
   *
   * Recomputed only when the cycle set changes, so every row is placed
   * against the same "now" -- a list measured across a tick could otherwise
   * show two current cycles, or none. It deliberately does not tick: a phase
   * boundary crossed while the page is open is picked up by the next fetch,
   * and a timer redrawing the whole list every second to catch a fortnightly
   * event is not a trade worth making.
   */
  const grouped = useMemo(() => groupByPhase(cycles, Date.now()), [cycles])

  const openComposer = useCallback(() => {
    setFormErrors(NO_ERRORS)
    setFormMessage(null)
    setIsComposerOpen(true)
  }, [])

  const closeComposer = useCallback(() => {
    setIsComposerOpen(false)
  }, [])

  const handleCreate = useCallback(
    (draft: CycleDraft) => {
      if (teamId === undefined) {
        return
      }

      void actions.createCycle(teamId, draft).then((outcome) => {
        if (outcome.status === 'ok') {
          setIsComposerOpen(false)
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
    [actions, teamId],
  )

  /** The number a new cycle is offered, one past the highest the team has. */
  const nextNumber = cycles.reduce((highest, cycle) => Math.max(highest, cycle.number), 0) + 1

  function renderRows(phase: readonly CycleFields[]) {
    return (
      <List label="Cycles">
        {phase.map((cycle) => (
          <ListRow interactive key={cycle.id}>
            <ListRowMain>
              <Link className={styles.rowLink} to={paths.cycle(cycle.id)}>
                {cycleTitle(cycle)}
              </Link>
            </ListRowMain>
            <ListRowMeta>
              {/* Two `<time>` elements would be more precise markup, but the
                  range reads as one fact and a screen reader announcing two
                  adjacent dates with no relation between them is worse. */}
              <span className={styles.dates}>{formatCycleRange(cycle)}</span>
              <span className={styles.rowNumber}>#{cycle.number}</span>
            </ListRowMeta>
          </ListRow>
        ))}
      </List>
    )
  }

  return (
    <>
      <PageHeader
        title="Cycles"
        description={team === undefined ? undefined : `${team.key} · ${team.name}`}
        actions={
          <Button variant="primary" onClick={openComposer} disabled={teamId === undefined}>
            New cycle
          </Button>
        }
      />

      <PageContent>
        {isLoadingTeams && (
          <div className={styles.skeletonStack} role="status" aria-busy="true">
            <VisuallyHidden as="div">Loading teams</VisuallyHidden>
            <Skeleton width="14rem" height="2rem" />
          </div>
        )}

        {teamsError !== null && (
          <ErrorState title="Could not load teams" description={teamsError} />
        )}

        {/* A workspace with no teams has no cycles to show, and says so
            rather than spinning: `cycles(teamId:)` cannot be asked without
            one. */}
        {!isLoadingTeams && teamsError === null && teams.length === 0 && (
          <EmptyState
            icon={<CycleIcon />}
            title="No teams in this workspace"
            description="Cycles belong to a team, so there is nothing to show until a team exists."
          />
        )}

        {teams.length > 0 && (
          <>
            <div className={styles.toolbar}>
              <div className={styles.teamPicker}>
                <label className={styles.label} htmlFor="cycles-team">
                  Team
                </label>
                <Select
                  id="cycles-team"
                  value={teamId ?? ''}
                  onChange={(event) => {
                    setSelectedTeamId(event.target.value)
                  }}
                >
                  {teams.map((candidate) => (
                    <option key={candidate.id} value={candidate.id}>
                      {candidate.key} · {candidate.name}
                    </option>
                  ))}
                </Select>
              </div>
            </div>

            {isLoading && (
              <div className={styles.skeletonStack} role="status" aria-busy="true">
                <VisuallyHidden as="div">Loading cycles</VisuallyHidden>
                {Array.from({ length: 4 }, (_unused, index) => (
                  <Skeleton key={index} width="100%" height="2.25rem" />
                ))}
              </div>
            )}

            {!isLoading && errorMessage !== null && (
              <ErrorState
                title="Could not load cycles"
                description={errorMessage}
                onRetry={retry}
              />
            )}

            {!isLoading && errorMessage === null && cycles.length === 0 && (
              <EmptyState
                icon={<CycleIcon />}
                title={`No cycles for ${team?.key ?? 'this team'}`}
                description="Cycles are the fortnights this team plans in. The first one you create will appear here."
                actions={
                  <Button variant="primary" onClick={openComposer}>
                    Create the first cycle
                  </Button>
                }
              />
            )}

            {!isLoading &&
              errorMessage === null &&
              cycles.length > 0 &&
              PHASES.map(({ key, title, empty }) => (
                <section
                  className={styles.phase}
                  key={key}
                  aria-labelledby={`cycles-${key}`}
                >
                  <div className={styles.phaseHead}>
                    <h2 className={styles.phaseTitle} id={`cycles-${key}`}>
                      {title}
                    </h2>
                    <span className={styles.phaseCount}>{grouped[key].length}</span>
                  </div>

                  {/* Each phase states its own emptiness. A section that
                      simply vanished would leave the reader unsure whether
                      there is no current cycle or whether the screen forgot
                      to render one. */}
                  {grouped[key].length === 0 ? (
                    <p className={styles.factEmpty}>{empty}</p>
                  ) : (
                    renderRows(grouped[key])
                  )}
                </section>
              ))}
          </>
        )}
      </PageContent>

      <Dialog
        open={isComposerOpen}
        onClose={closeComposer}
        title="New cycle"
        description={
          team === undefined ? undefined : `For ${team.key} · ${team.name}`
        }
      >
        <CycleForm
          initial={{ number: nextNumber, name: null, startsAt: '', endsAt: '' }}
          submitLabel="Create cycle"
          isSaving={actions.isSaving}
          onCancel={closeComposer}
          onSubmit={handleCreate}
          errors={formErrors}
          errorMessage={formMessage}
        />
      </Dialog>
    </>
  )
}
