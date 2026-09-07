import { PageContent, PageHeader } from '../../app/layout'
import { useWorkspace } from '../../app/workspace'
import {
  EmptyState,
  ErrorState,
  IssueIcon,
  Skeleton,
  VisuallyHidden,
} from '../../components'
import { useIssueList, useWorkspaceContext } from '../issues/api'
import { IssueRows, ListFooter } from '../screens'
import styles from '../screens.module.css'

/** Six bars: a hint about layout, not a promise about how many rows are coming. */
const SKELETON_ROWS = 6

/**
 * The issues assigned to the signed-in viewer, across every team.
 *
 * ## The filter runs in the browser, and the screen says so
 *
 * `issues(workspaceSlug:, teamId:, first:, after:)` is the whole filter
 * surface the API offers. There is no `assigneeId` argument, so "assigned to
 * me" cannot be asked of the server: this loads a page of the *workspace's*
 * issues and keeps the ones whose `assigneeId` is the viewer's.
 *
 * That has a consequence the user has to be told about, because it is
 * invisible otherwise: an issue assigned to you that is older than the last
 * row loaded is not on this screen, and nothing about an unfiltered-looking
 * list would suggest it. So the footnote states what the list is matched
 * against, the empty state distinguishes "none among those loaded" from
 * "none at all", and the button says it loads more of the workspace rather
 * than more of your issues. `features/projects` makes the same admission for
 * the same reason -- the API has no per-project filter either.
 *
 * When `issues(assigneeId:)` arrives, this screen loses the footnote and the
 * filter and gains a variable, and nothing else about it changes.
 *
 * ## Landmarks
 *
 * `AppLayout` owns the `<main>` landmark and `PageHeader` owns the page's
 * only `<h1>`, so this renders a fragment and adds neither.
 */
export function MyIssuesPage() {
  const { viewer } = useWorkspace()

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
  } = useIssueList()

  // The lookups every row needs in order to draw a status glyph and an
  // avatar from the raw UUIDs it carries. A failure here costs those two
  // columns and nothing else, so it is not raised to the page.
  const { stateById, memberById } = useWorkspaceContext()

  const viewerId = viewer?.id ?? null

  const mine =
    viewerId === null
      ? []
      : issues.filter((issue) => issue.assigneeId === viewerId)

  const hasMine = mine.length > 0
  const showError = !isLoadingFirstPage && errorMessage !== null && issues.length === 0

  return (
    <>
      <PageHeader
        title="My Issues"
        description="Issues assigned to you, across every team in this workspace."
      />

      <PageContent>
        {viewerId === null ? (
          /* The shell publishes a null viewer only when the session ended
             under us. Filtering by "me" is not a question that has an answer
             in that state, and an empty list would be the wrong one. */
          <ErrorState
            title="Could not tell who is signed in"
            description="Your session may have ended. Reload the page to sign in again."
          />
        ) : (
          <div className={styles.stack}>
            {isLoadingFirstPage && (
              <div className={styles.skeletonStack} role="status" aria-busy="true">
                {/* `Skeleton` is `aria-hidden` by design -- a picture of text
                    that does not exist yet -- so the announcement belongs on
                    one live region for the whole list, which is this element. */}
                <VisuallyHidden as="div">Loading your issues</VisuallyHidden>
                {Array.from({ length: SKELETON_ROWS }, (_unused, index) => (
                  <Skeleton key={index} width="100%" height="2.25rem" />
                ))}
              </div>
            )}

            {/* The whole-list error panel replaces the list only when there is
                no list to replace. Rows already loaded stay, and a later
                failure is reported in the footer -- throwing away good data to
                display an error is not error handling. */}
            {showError && errorMessage !== null && (
              <ErrorState
                title="Could not load issues"
                description={errorMessage}
                onRetry={isRefreshing ? undefined : retry}
              />
            )}

            {!isLoadingFirstPage && !showError && (
              <>
                {/* A live region, because the number that matters changes
                    without the user doing anything visible: every "load more"
                    can pull more of your issues into a list that otherwise
                    just grows. */}
                <p className={styles.footnote} role="status">
                  {mine.length} of {issues.length} loaded{' '}
                  {issues.length === 1 ? 'issue' : 'issues'} assigned to you.
                </p>

                {hasMine ? (
                  <IssueRows
                    label="Issues assigned to you"
                    issues={mine}
                    stateById={stateById}
                    memberById={memberById}
                  />
                ) : (
                  <EmptyState
                    icon={<IssueIcon />}
                    title="Nothing assigned to you"
                    description={
                      hasNextPage
                        ? 'None among the issues loaded so far. Older issues may be assigned to you -- load more below.'
                        : 'Every issue in this workspace has been checked; none is assigned to you.'
                    }
                  />
                )}

                <p className={styles.footnote}>
                  The API offers no assignee filter, so this list is matched in
                  the browser against the {issues.length} most recent{' '}
                  {issues.length === 1 ? 'issue' : 'issues'} in the workspace.
                  Loading more loads more of the workspace, not more of your
                  issues.
                </p>

                <ListFooter
                  noun="issue"
                  loadedCount={issues.length}
                  hasNextPage={hasNextPage}
                  isLoadingMore={isLoadingMore}
                  errorMessage={loadMoreErrorMessage}
                  onLoadMore={loadMore}
                  moreLabel="Load more workspace issues"
                />
              </>
            )}
          </div>
        )}
      </PageContent>
    </>
  )
}
