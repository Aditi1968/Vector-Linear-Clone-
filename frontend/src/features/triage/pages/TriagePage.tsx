import { useCallback, useMemo, useState } from 'react'

import { PageContent, PageHeader } from '../../../app/layout'
import {
  Button,
  Dialog,
  EmptyState,
  ErrorState,
  InboxIcon,
  List,
  Select,
  Skeleton,
  VisuallyHidden,
} from '../../../components'
import { useWorkspaceContext } from '../../issues/api'
import { useTriageActions, useTriageQueue } from '../api'
import type { TriageOutcome } from '../api'
import { TriageRow } from '../components/TriageRow'
import styles from '../triage.module.css'

/**
 * The triage queue: work that has arrived but has not been accepted.
 *
 * ## Why there is a team picker and not a workspace-wide queue
 *
 * `triageIssues(workspaceSlug:, teamId:)` requires a team, and so does
 * `triageCount`. That is the schema showing through rather than an API
 * inconvenience: a triage queue is one team's inbox, and accepting an issue
 * means putting it in a state on *that team's* board. The picker is the first
 * control on the screen for the same reason the cycles screen carries one.
 *
 * The team is component state rather than a URL segment, matching
 * `features/cycles`: `/acme/triage` has to resolve for anyone who follows it,
 * and a slug plus a team UUID is not a URL a person can type or share.
 *
 * ## Where the lookups come from
 *
 * `useWorkspaceContext()` -- the issues feature's shared document. It answers
 * teams (with their `workflowStates`), members and projects in one request
 * for the whole screen, and it is the same cache entry every other screen
 * reads, so mounting it here usually costs nothing. Resolving a state name or
 * a member name per row would be 25 requests a page.
 *
 * ## What a refusal looks like
 *
 * There is no `PermissionState` on this screen, and that is deliberate. The
 * backend publishes exactly three error codes -- `BAD_USER_INPUT`,
 * `UNAUTHENTICATED`, `NOT_FOUND` (`PUBLIC_ERROR_CODES` in
 * `app/graphql/schema.py`) -- and no `FORBIDDEN`. An issue the caller may not
 * act on comes back as the same `NOT_FOUND` a nonexistent one does, on
 * purpose, so that the response cannot be used to probe for what exists.
 * A screen therefore cannot tell a refusal from a missing row, and rendering
 * "you do not have permission" would be a guess. Refusals are shown as what
 * they arrive as: a structured message about the action that failed.
 * `UNAUTHENTICATED` never reaches here at all -- `sessionExpiryLink` turns it
 * into the sign-in redirect.
 */
export function TriagePage() {
  const { teams, members, isLoading: isLoadingContext } = useWorkspaceContext()
  const [selectedTeamId, setSelectedTeamId] = useState<string | null>(null)

  // The first team until someone chooses otherwise. A default, not a
  // decision: the screen needs *a* team to ask about, and the picker beside
  // it is how the choice is made and how it stays visible.
  const teamId = selectedTeamId ?? teams[0]?.id
  const team = teams.find((candidate) => candidate.id === teamId)

  const { rows, totalCount, hasNextPage, isLoading, errorMessage, retry } =
    useTriageQueue(teamId)
  const actions = useTriageActions()

  /** A failed or refused action, shown once above the list. */
  const [actionError, setActionError] = useState<string | null>(null)
  /** The issue the duplicate dialog is collecting a target for. */
  const [duplicateOf, setDuplicateOf] = useState<string | null>(null)
  const [duplicateTarget, setDuplicateTarget] = useState('')

  const otherTeams = useMemo(
    () => teams.filter((candidate) => candidate.id !== teamId),
    [teamId, teams],
  )

  /**
   * One instant for the whole render, so every age is measured against the
   * same "now". Recomputed only when the rows change; a timer redrawing the
   * list every second to keep "3 minutes ago" honest is not a trade worth
   * making for a queue that is refetched on every action anyway.
   */
  const now = useMemo(() => Date.now(), [rows])

  /**
   * Run one action and report whatever came back.
   *
   * The two failure channels are flattened to a single sentence here, and
   * that is a considered narrowing rather than laziness: `rejected` carries
   * a `field`, but every field it can name -- `issueId`, `workflowStateId`,
   * `duplicateOfId`, `teamId` -- is a menu choice rather than an input the
   * user can correct in place. There is nothing to attach the message to, so
   * it is stated once, above the list.
   */
  const run = useCallback((action: Promise<TriageOutcome>) => {
    void action.then((outcome) => {
      if (outcome.status === 'ok') {
        setActionError(null)
        return
      }

      setActionError(
        outcome.status === 'failed'
          ? outcome.message
          : (outcome.errors[0]?.message ?? 'That action was refused.'),
      )
    })
  }, [])

  const handleAccept = useCallback(
    (issueId: string, workflowStateId: string) => {
      run(actions.accept(issueId, workflowStateId))
    },
    [actions, run],
  )

  const handleDecline = useCallback(
    (issueId: string) => {
      run(actions.decline(issueId))
    },
    [actions, run],
  )

  const handleChangeTeam = useCallback(
    (issueId: string, nextTeamId: string) => {
      run(actions.changeTeam(issueId, nextTeamId))
    },
    [actions, run],
  )

  const handleSetPriority = useCallback(
    (issueId: string, priority: number) => {
      run(actions.updateIssue(issueId, { priority }))
    },
    [actions, run],
  )

  const handleAssign = useCallback(
    (issueId: string, assigneeId: string | null) => {
      run(actions.updateIssue(issueId, { assigneeId }))
    },
    [actions, run],
  )

  const openDuplicate = useCallback((issueId: string) => {
    setDuplicateTarget('')
    setDuplicateOf(issueId)
  }, [])

  const closeDuplicate = useCallback(() => {
    setDuplicateOf(null)
  }, [])

  const confirmDuplicate = useCallback(() => {
    if (duplicateOf === null || duplicateTarget === '') {
      return
    }

    run(actions.markDuplicate(duplicateOf, duplicateTarget))
    setDuplicateOf(null)
  }, [actions, duplicateOf, duplicateTarget, run])

  /** The rows a duplicate can point at: everything in the queue but itself. */
  const duplicateCandidates = rows.filter((row) => row.issue.id !== duplicateOf)

  const isBusy = isLoadingContext || isLoading

  return (
    <>
      <PageHeader
        title="Triage"
        description={team === undefined ? undefined : `${team.key} · ${team.name}`}
      />

      <PageContent>
        {isLoadingContext && (
          <div className={styles.skeletonStack} role="status" aria-busy="true">
            <VisuallyHidden as="div">Loading teams</VisuallyHidden>
            <Skeleton width="14rem" height="2rem" />
          </div>
        )}

        {/* A workspace with no teams has no queue to show, and says so rather
            than spinning: `triageIssues(teamId:)` cannot be asked without
            one. */}
        {!isLoadingContext && teams.length === 0 && (
          <EmptyState
            icon={<InboxIcon />}
            title="No teams in this workspace"
            description="A triage queue belongs to a team, so there is nothing to show until a team exists."
          />
        )}

        {teams.length > 0 && (
          <>
            <div className={styles.toolbar}>
              <div className={styles.teamPicker}>
                <label className={styles.label} htmlFor="triage-team">
                  Team
                </label>
                <Select
                  id="triage-team"
                  value={teamId ?? ''}
                  onChange={(event) => {
                    setActionError(null)
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

            {actionError !== null && (
              <p className={styles.actionError} role="alert">
                {actionError}
              </p>
            )}

            {isBusy && (
              <div className={styles.skeletonStack} role="status" aria-busy="true">
                <VisuallyHidden as="div">Loading the triage queue</VisuallyHidden>
                {Array.from({ length: 5 }, (_unused, index) => (
                  <Skeleton key={index} width="100%" height="2.25rem" />
                ))}
              </div>
            )}

            {!isBusy && errorMessage !== null && (
              <ErrorState
                title="Could not load the triage queue"
                description={errorMessage}
                onRetry={retry}
              />
            )}

            {!isBusy && errorMessage === null && rows.length === 0 && (
              <EmptyState
                icon={<InboxIcon />}
                title={`Nothing waiting for ${team?.key ?? 'this team'}`}
                description="Issues filed without a state land here to be accepted onto the board or declined. An empty queue means everything has been decided."
              />
            )}

            {!isBusy && errorMessage === null && rows.length > 0 && (
              <>
                {/*
                  What is on screen against what is in the queue. `triageCount`
                  is the whole queue and this page is the oldest 25 of it, so
                  the two numbers are stated whenever they differ -- see
                  ../api/queries.ts for why there is no "Load more" here.
                */}
                <p className={styles.countLine}>
                  {hasNextPage ? (
                    <>
                      Showing the <span className={styles.count}>{rows.length}</span>{' '}
                      oldest of <span className={styles.count}>{totalCount}</span>{' '}
                      waiting. Deal with these and the next will follow.
                    </>
                  ) : (
                    <>
                      <span className={styles.count}>{totalCount}</span>{' '}
                      {totalCount === 1 ? 'issue is' : 'issues are'} waiting, oldest
                      first.
                    </>
                  )}
                </p>

                <List label={`Issues waiting for ${team?.key ?? 'this team'}`}>
                  {rows.map((row) => (
                    <TriageRow
                      isSaving={actions.isSaving}
                      key={row.issue.id}
                      members={members}
                      now={now}
                      onAccept={handleAccept}
                      onAssign={handleAssign}
                      onChangeTeam={handleChangeTeam}
                      onDecline={handleDecline}
                      onMarkDuplicate={openDuplicate}
                      onSetPriority={handleSetPriority}
                      otherTeams={otherTeams}
                      row={row}
                      states={team?.workflowStates ?? []}
                    />
                  ))}
                </List>
              </>
            )}
          </>
        )}
      </PageContent>

      <Dialog
        open={duplicateOf !== null}
        onClose={closeDuplicate}
        title="Mark as duplicate"
        description="The issue will be closed as a duplicate of the one you choose."
      >
        <div className={styles.form}>
          <div className={styles.field}>
            <label className={styles.label} htmlFor="triage-duplicate-of">
              Duplicate of
            </label>
            <Select
              id="triage-duplicate-of"
              value={duplicateTarget}
              onChange={(event) => {
                setDuplicateTarget(event.target.value)
              }}
            >
              <option value="">Choose an issue</option>
              {duplicateCandidates.map((candidate) => (
                <option key={candidate.issue.id} value={candidate.issue.id}>
                  {candidate.issue.identifier} · {candidate.issue.title}
                </option>
              ))}
            </Select>
            {/*
              Said plainly rather than left to be discovered. The choices are
              the queue rows this screen has loaded, so an issue already
              accepted -- or one further down a queue that did not fit on this
              page -- cannot be picked here. `triageMarkDuplicate` accepts any
              issue in the workspace; it is this picker that is narrow, and a
              reader who does not know that will think the target is gone.
            */}
            <p className={styles.hint}>
              Only issues loaded in this queue can be chosen here.
            </p>
          </div>

          <div className={styles.formActions}>
            <Button onClick={closeDuplicate}>Cancel</Button>
            <Button
              disabled={duplicateTarget === '' || actions.isSaving}
              onClick={confirmDuplicate}
              variant="primary"
            >
              Mark as duplicate
            </Button>
          </div>
        </div>
      </Dialog>
    </>
  )
}
