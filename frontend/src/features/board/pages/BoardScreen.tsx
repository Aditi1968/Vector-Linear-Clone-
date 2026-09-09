import { useCallback, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'

import { PageContent, PageHeader } from '../../../app/layout'
import {
  Button,
  EmptyState,
  ErrorState,
  IssuesIcon,
  Skeleton,
  Spinner,
  TeamIcon,
  VisuallyHidden,
} from '../../../components'
import { useTeamCycles, useWorkspaceContext } from '../../issues/api'
import type { IssueRowFields } from '../../issues/api'
import { useBoardIssues, useBoardLabels, useMoveIssue } from '../api'
import styles from '../board.module.css'
import { BoardColumn } from '../components/BoardColumn'
import { BoardControls } from '../components/BoardControls'
import { buildColumns, filterOptions } from '../lib/arrange'
import {
  applyBoardView,
  hasActiveFilter,
  parseBoardView,
  toIssueFilter,
  toIssueOrder,
} from '../lib/viewState'
import type { BoardView } from '../lib/viewState'

/** No workflow states, as a stable identity so the memos below do not churn. */
const NO_STATES: readonly [] = []

/** Today as `YYYY-MM-DD` in the viewer's own timezone, for overdue comparisons. */
function localToday(): string {
  const now = new Date()

  return [
    now.getFullYear(),
    String(now.getMonth() + 1).padStart(2, '0'),
    String(now.getDate()).padStart(2, '0'),
  ].join('-')
}

/**
 * The board: one team's issues, in columns.
 *
 * ## The columns are the team's, not this file's
 *
 * There is no Todo/Doing/Done anywhere in this feature. A column is a
 * `WorkflowState` the server returned for the selected team, drawn in
 * `position` order with the team's own name and the category's own glyph. That
 * is also why the board is team-scoped and carries a team picker: workflow
 * states belong to a team, so "the workspace's board" is not a thing that
 * exists, and mixing two teams' issues would produce cards whose state matches
 * no column on screen.
 *
 * ## The view is in the URL
 *
 * Filters, sorting and grouping round-trip through the query string
 * (../lib/viewState.ts), so a board is a link: it survives a refresh, the back
 * button undoes a filter, and "the urgent unassigned work on ENG" is
 * something you can paste into a message.
 *
 * ## What the server does, and the one thing it still cannot
 *
 * Every filter control is a key of `IssueFilterInput` and the sort control is
 * `IssueOrderInput`, so the cards that arrive are the matching ones, in
 * order, and `totalCount` is how many match. Grouping stays in the browser
 * because there is no grouping argument -- and that is right: a grouped board
 * needs all the matching issues anyway.
 *
 * What server-side filtering does NOT do is make the answer complete. It is
 * still one keyset-paginated page of the matching issues, so while there is
 * another page the screen says how many of the total are on it. Once there is
 * not, it says nothing.
 */
export function BoardScreen() {
  const [searchParams, setSearchParams] = useSearchParams()
  const view = useMemo(() => parseBoardView(searchParams), [searchParams])

  const {
    teams,
    members,
    projects,
    memberById,
    isLoading: isLoadingContext,
    errorMessage: contextError,
    retry: retryContext,
  } = useWorkspaceContext()

  /**
   * The team whose board this is.
   *
   * The URL names a team by key; falling back to the first team is what makes
   * `/acme/board` a working address rather than an error. A key that names no
   * team is NOT resolved to the first team, because that would show one team's
   * board under a URL claiming another's -- it is reported below instead.
   */
  const team =
    view.team === null
      ? teams[0]
      : teams.find((candidate) => candidate.key === view.team)

  /*
    `teams.length > 0` is load-bearing, not a tidiness check.

    Without it this is true from the first frame: `teams` is empty while the
    lookup is in flight, so `team` is undefined, so the screen announces "No
    team with the key ENG" *underneath its own loading spinner* -- and goes on
    announcing it forever if the request fails. Both are claims about which
    teams the workspace has, made by a screen that has not been told.

    Once the list has actually arrived and is empty, "no teams at all" is the
    truer statement and the empty state below says it instead.
  */
  const isUnknownTeam = view.team !== null && teams.length > 0 && team === undefined
  const states = team?.workflowStates ?? NO_STATES

  /**
   * The view, as the two arguments the server takes.
   *
   * Memoised on the view and the team because these are query variables: a
   * fresh object every render is a fresh set of variables every render, which
   * Apollo would read as a different question.
   */
  const teamId = team?.id
  const filter = useMemo(
    () => (teamId === undefined ? undefined : toIssueFilter(view, teamId)),
    [teamId, view],
  )
  const orderBy = useMemo(() => toIssueOrder(view), [view])

  const {
    issues,
    totalCount,
    hasNextPage,
    isLoadingFirstPage,
    isLoadingMore,
    errorMessage,
    loadMoreErrorMessage,
    loadMore,
    retry,
  } = useBoardIssues(filter, orderBy)

  const { moveIssue } = useMoveIssue()

  /** What was just moved, announced politely; and what failed, announced assertively. */
  const [announcement, setAnnouncement] = useState('')
  const [moveError, setMoveError] = useState<string | null>(null)

  /** The card being dragged, and the card whose move is in flight. */
  const [draggingId, setDraggingId] = useState<string | null>(null)
  const [movingId, setMovingId] = useState<string | null>(null)

  const columns = useMemo(
    () => buildColumns(issues, view, { states, memberById }),
    [issues, memberById, states, view],
  )

  // The team's cycles and the workspace's labels, for two of the pickers. The
  // other two read lists the workspace context already holds.
  const cycles = useTeamCycles(teamId)
  const labels = useBoardLabels()

  const options = useMemo(
    () => filterOptions({ members, projects, labels, cycles }),
    [cycles, labels, members, projects],
  )

  /**
   * Where a card can be moved, in the team's own board order.
   *
   * The states in every grouping, not only in the status one: a status change
   * is the board's one write and a view choice must not remove it. What the
   * grouping does decide is whether *dragging* and the arrow keys apply, which
   * each column answers for itself.
   */
  const moveTargets = useMemo(
    () => [...states].sort((left, right) => left.position - right.position),
    [states],
  )

  const updateView = useCallback(
    (next: BoardView) => {
      // `replace`, so that adjusting four filters in a row leaves one history
      // entry rather than four the back button has to be pressed through. The
      // URL is still the state, and still shareable; it is only the number of
      // stops on the way here that is being kept sane.
      setSearchParams((current) => applyBoardView(current, next), { replace: true })
    },
    [setSearchParams],
  )

  const handleMove = useCallback(
    (issue: IssueRowFields, workflowStateId: string) => {
      if (issue.workflowStateId === workflowStateId) {
        return
      }

      const target = states.find((state) => state.id === workflowStateId)

      if (target === undefined) {
        return
      }

      setMoveError(null)
      setMovingId(issue.id)

      // Announced before the server has answered, which matches what the user
      // sees: the optimistic update has already moved the card. A failure
      // corrects both, in the assertive region below.
      setAnnouncement(`${issue.identifier} moved to ${target.name}`)

      void moveIssue(issue, workflowStateId).then(
        (failure) => {
          setMovingId(null)

          if (failure !== null) {
            setAnnouncement('')
            setMoveError(
              `${issue.identifier} could not be moved to ${target.name}. ${failure}`,
            )
          }
        },
        () => {
          // `moveIssue` resolves rather than rejects, so this is unreachable
          // -- and is written out so that a future change to it cannot turn a
          // failed move into a card stuck in its moving state forever.
          setMovingId(null)
        },
      )
    },
    [moveIssue, states],
  )

  const handleDrop = useCallback(
    (issueId: string, workflowStateId: string) => {
      setDraggingId(null)

      const issue = issues.find((candidate) => candidate.id === issueId)

      // A drop carrying an id this board does not hold -- text dragged in from
      // another window, or a card from a board in another tab. Ignored rather
      // than sent to the server.
      if (issue !== undefined) {
        handleMove(issue, workflowStateId)
      }
    },
    [handleMove, issues],
  )

  const handleDragEnd = useCallback(() => {
    setDraggingId(null)
  }, [])

  const today = localToday()
  const loadedCount = issues.length
  const isFiltered = hasActiveFilter(view)
  const isEmptyBoard =
    !isLoadingFirstPage && errorMessage === null && loadedCount === 0

  return (
    <>
      {/* The team is a readout and not a subtitle: `ENG · Engineering` is the
        * scope this screen is measuring, said in numbers and keys, which is
        * exactly what the shell's mono legend beside the title is for. As a
        * description it was a sentence that is not one. */}
      <PageHeader
        readout={team === undefined ? undefined : `${team.key} · ${team.name}`}
        title="Board"
      />

      <PageContent className={styles.screen}>
        {isLoadingContext && teams.length === 0 && (
          <p className={styles.notice} role="status">
            <Spinner />
            Loading teams...
          </p>
        )}

        {/* The team lookup failed. How many teams this workspace has is
          * therefore not something this screen knows, and the empty state
          * below would assert it. */}
        {!isLoadingContext && contextError !== null && teams.length === 0 && (
          <ErrorState
            description={contextError}
            onRetry={retryContext}
            title="Could not load teams"
          />
        )}

        {/* A board is a team's workflow states. Without a team there is
          * nothing to draw columns from, and this says that rather than
          * rendering an empty frame. Only once the request answered with
          * none -- an empty array from a failed request is not an answer. */}
        {!isLoadingContext && contextError === null && teams.length === 0 && (
          <EmptyState
            description="A board is one team's workflow states, so there is nothing to show until a team exists."
            icon={<TeamIcon />}
            title="No teams in this workspace"
          />
        )}

        {teams.length > 0 && (
          <BoardControls
            onChange={updateView}
            options={options}
            states={states}
            teams={teams}
            view={view}
          />
        )}

        {isUnknownTeam && (
          <EmptyState
            description="The address names a team this workspace does not have. Pick one above."
            icon={<TeamIcon />}
            title={`No team with the key ${view.team ?? ''}`}
          />
        )}

        {team !== undefined && (
          <>
            {/* Only while the board is partial. Filtering and sorting happen
              * on the server now, so with every matching issue loaded there
              * is nothing here that the columns do not already say. A live
              * region, because the numbers change as pages arrive. */}
            {(hasNextPage || isLoadingMore) && (
              <p className={styles.scopeNote} role="status">
                Showing {loadedCount} of {totalCount}{' '}
                {totalCount === 1 ? 'issue' : 'issues'}, so a column may hold
                fewer than it will.
                {hasNextPage && !isLoadingMore && (
                  <Button className={styles.loadMore} onClick={loadMore} size="sm">
                    Load more
                  </Button>
                )}
                {isLoadingMore && (
                  <span className={styles.loadingMore}>
                    <Spinner />
                    Loading more issues...
                  </span>
                )}
              </p>
            )}

            {loadMoreErrorMessage !== null && (
              <p className={styles.inlineError} role="alert">
                {loadMoreErrorMessage}
                <Button onClick={loadMore} size="sm">
                  Try again
                </Button>
              </p>
            )}

            {/*
              The polite region carries every completed move; the assertive one
              carries the failures. Both are always in the DOM, because a live
              region that is created at the moment it has something to say is
              not announced by most screen readers.
            */}
            <div role="status">
              <VisuallyHidden as="div">{announcement}</VisuallyHidden>
            </div>

            {moveError !== null && (
              <p className={styles.inlineError} role="alert">
                {moveError}
              </p>
            )}

            {isLoadingFirstPage && (
              <div aria-busy="true" className={styles.skeletonBoard} role="status">
                <VisuallyHidden as="div">Loading board</VisuallyHidden>
                {[0, 1, 2].map((index) => (
                  <div className={styles.skeletonColumn} key={index}>
                    <Skeleton width="8ch" />
                    <Skeleton height="var(--space-10)" />
                    <Skeleton height="var(--space-10)" />
                  </div>
                ))}
              </div>
            )}

            {/* The whole-board error replaces the board only when there is no
              * board to replace: cards already loaded stay, and a later
              * failure is reported beside them. */}
            {!isLoadingFirstPage && errorMessage !== null && loadedCount === 0 && (
              <ErrorState
                description={errorMessage}
                onRetry={retry}
                title="Could not load this board"
              />
            )}

            {/* An empty board is now two different situations, and the server
              * filter is what separates them: nothing matches the filters, or
              * nothing has been filed at all. Saying the second when the first
              * is true would send someone looking for a bug. */}
            {isEmptyBoard && (
              <EmptyState
                description={
                  isFiltered
                    ? 'No issue on this board matches the filters above.'
                    : 'Nothing has been filed against this team yet.'
                }
                icon={<IssuesIcon />}
                title={isFiltered ? 'No matching issues' : `No issues for ${team.key}`}
              />
            )}

            {loadedCount > 0 && (
              <div className={styles.board}>
                {columns.map((column) => (
                  <BoardColumn
                    column={column}
                    draggingId={draggingId}
                    isPartial={hasNextPage}
                    key={column.id}
                    memberById={memberById}
                    moveTargets={moveTargets}
                    movingId={movingId}
                    onDragEnd={handleDragEnd}
                    onDragStart={setDraggingId}
                    onDrop={handleDrop}
                    onMove={handleMove}
                    today={today}
                  />
                ))}
              </div>
            )}
          </>
        )}
      </PageContent>
    </>
  )
}
