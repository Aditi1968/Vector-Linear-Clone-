import { Link } from 'react-router-dom'

import { PageContent, PageHeader } from '../../app/layout'
import { useAppPaths } from '../../app/routes'
import {
  EmptyState,
  ErrorState,
  IssuesIcon,
  Skeleton,
  VisuallyHidden,
} from '../../components'
import { useWorkspaceContext } from '../issues/api'
import { IssueRows, ListFooter } from '../screens'
import shared from '../screens.module.css'
import styles from './Teams.module.css'
import { useTeamByKey, useTeamIssues } from './api'
import { TeamStateScreen } from './TeamStates'

/** Six bars: a hint about layout, not a promise about how many rows are coming. */
const SKELETON_ROWS = 6

/**
 * One team's issues.
 *
 * ## This list is the team's, and the server says so
 *
 * `filter: { teamId }` is a real server-side filter, so every row here is the
 * team's and the page ends where the connection ends -- no caveat about what
 * happened to be loaded, because there is nothing to caveat.
 *
 * The team is addressed by key and resolved through `teams(workspaceSlug:)`;
 * a key that names nothing gets a clean not-found rather than an empty list
 * that looks like a team with no work. See ./api.ts and ./TeamStates.tsx.
 *
 * Grouping and sorting are deliberately absent here: they belong to the board
 * view, which is another screen and another owner. This is the flat list.
 */
export function TeamIssuesPage() {
  const paths = useAppPaths()
  const resolution = useTeamByKey()

  const teamId = resolution.status === 'found' ? resolution.team.id : null

  // Called unconditionally and skipped internally while the key is still
  // resolving: a hook cannot sit behind an early return.
  const {
    issues,
    hasNextPage,
    isLoadingFirstPage,
    isLoadingMore,
    isRefreshing,
    errorMessage,
    loadMoreErrorMessage,
    loadMore,
    retry,
  } = useTeamIssues(teamId)

  const { stateById, memberById } = useWorkspaceContext()

  if (resolution.status !== 'found') {
    return <TeamStateScreen resolution={resolution} />
  }

  const { team } = resolution
  const hasIssues = issues.length > 0
  const showError = !isLoadingFirstPage && errorMessage !== null && !hasIssues

  return (
    <>
      <PageHeader
        title={`${team.key} issues`}
        description={`Every issue filed against ${team.name}.`}
        actions={
          <Link className={styles.headerAction} to={paths.team(team.key)}>
            Team overview
          </Link>
        }
      />

      <PageContent>
        <div className={shared.stack}>
          {isLoadingFirstPage && (
            <div className={shared.skeletonStack} role="status" aria-busy="true">
              {/* `Skeleton` is `aria-hidden` by design, so the announcement
                  belongs on one live region for the whole list. */}
              <VisuallyHidden as="div">Loading {team.key} issues</VisuallyHidden>
              {Array.from({ length: SKELETON_ROWS }, (_unused, index) => (
                <Skeleton key={index} width="100%" height="2.25rem" />
              ))}
            </div>
          )}

          {/* The whole-list error panel replaces the list only when there is
              no list to replace. Rows already loaded stay, and a later failure
              is reported in the footer. */}
          {showError && errorMessage !== null && (
            <ErrorState
              title={`Could not load ${team.key} issues`}
              description={errorMessage}
              onRetry={isRefreshing ? undefined : retry}
            />
          )}

          {!isLoadingFirstPage && !showError && !hasIssues && (
            <EmptyState
              icon={<IssuesIcon />}
              title={`No issues on ${team.key} yet`}
              description={`The first issue filed against ${team.name} will appear here.`}
            />
          )}

          {hasIssues && (
            <>
              <IssueRows
                label={`${team.key} issues`}
                issues={issues}
                stateById={stateById}
                memberById={memberById}
              />

              <ListFooter
                noun="issue"
                loadedCount={issues.length}
                hasNextPage={hasNextPage}
                isLoadingMore={isLoadingMore}
                errorMessage={loadMoreErrorMessage}
                onLoadMore={loadMore}
              />
            </>
          )}
        </div>
      </PageContent>
    </>
  )
}
