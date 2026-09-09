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
 * ## The server does the filtering
 *
 * `filter: { assigneeId }`, so every row that arrives is the viewer's and
 * `totalCount` is how many there are rather than how many were noticed among
 * a page of the workspace. What remains true is that the answer is still
 * paginated: the footer says how many of the total are on screen.
 *
 * The one trap in that filter, spelled out because it is silent: an absent
 * `assigneeId` is "any assignee" and an explicit null is "unassigned". The
 * query is therefore skipped rather than sent while the viewer is unknown --
 * passing `assigneeId: viewer?.id ?? null` would quietly show somebody else's
 * unassigned work under a heading that says it is yours.
 *
 * ## Landmarks
 *
 * `AppLayout` owns the `<main>` landmark and `PageHeader` owns the page's
 * only `<h1>`, so this renders a fragment and adds neither.
 */
export function MyIssuesPage() {
  const { viewer } = useWorkspace()
  const viewerId = viewer?.id ?? null

  const {
    issues,
    totalCount,
    hasNextPage,
    isLoadingFirstPage,
    isLoadingMore,
    isRefreshing,
    errorMessage,
    loadMoreErrorMessage,
    loadMore,
    retry,
  } = useIssueList(
    viewerId === null
      ? { skip: true }
      : { filter: { assigneeId: viewerId } },
  )

  // The lookups every row needs in order to draw a status glyph and an
  // avatar from the raw UUIDs it carries. A failure here costs those two
  // columns and nothing else, so it is not raised to the page.
  const { stateById, memberById } = useWorkspaceContext()

  const hasMine = issues.length > 0
  const showError = !isLoadingFirstPage && errorMessage !== null && issues.length === 0

  return (
    <>
      {/* The readout is the screen reporting its own state in numbers, which
        * the shell paints as a mono legend beside the title. Withheld until
        * the first page has landed: `totalCount` is 0 before the server has
        * answered, and "0 ASSIGNED" is a claim rather than a placeholder. */}
      <PageHeader
        title="My Issues"
        readout={
          viewerId === null || isLoadingFirstPage || showError
            ? undefined
            : `${String(totalCount)} assigned`
        }
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
                  /* `--row-height` and not a literal: the bars are standing in
                     for rows, and the shell re-points that token when the
                     density changes -- a fixed height would make the list
                     jump the moment the real rows arrived. */
                  <Skeleton key={index} width="100%" height="var(--row-height)" />
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
                {/* A live region, because the number changes without the user
                    doing anything visible: every "load more" brings more of
                    the total onto the screen. */}
                <p className={styles.footnote} role="status">
                  {hasNextPage
                    ? `Showing ${String(issues.length)} of ${String(totalCount)} issues assigned to you.`
                    : `${String(totalCount)} ${totalCount === 1 ? 'issue' : 'issues'} assigned to you.`}
                </p>

                {hasMine ? (
                  <IssueRows
                    label="Issues assigned to you"
                    issues={issues}
                    stateById={stateById}
                    memberById={memberById}
                  />
                ) : (
                  <EmptyState
                    icon={<IssueIcon />}
                    title="Nothing assigned to you"
                    description="No issue in this workspace is assigned to you."
                  />
                )}

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
        )}
      </PageContent>
    </>
  )
}
