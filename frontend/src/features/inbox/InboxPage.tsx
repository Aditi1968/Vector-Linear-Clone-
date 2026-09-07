import { useState } from 'react'
import { Link } from 'react-router-dom'

import { PageContent, PageHeader } from '../../app/layout'
import { useAppPaths } from '../../app/routes'
import {
  Badge,
  Button,
  EmptyState,
  ErrorState,
  InboxIcon,
  List,
  ListRow,
  ListRowMain,
  ListRowMeta,
  SegmentedControl,
  Skeleton,
  VisuallyHidden,
} from '../../components'
import { memberLabel, useWorkspaceContext } from '../issues/api'
import type { WorkspaceMember } from '../issues/api'
import { formatRelative } from '../issues/lib/dates'
import { ListFooter } from '../screens'
import styles from '../screens.module.css'
import { useInbox } from './api'
import type { InboxNotification } from './api'

/** Six bars: a hint about layout, not a promise about how many rows are coming. */
const SKELETON_ROWS = 6

type Filter = 'all' | 'unread'

const FILTERS = [
  { value: 'all', label: 'All' },
  { value: 'unread', label: 'Unread' },
] as const satisfies readonly { value: Filter; label: string }[]

/**
 * What happened, in a sentence.
 *
 * `NotificationKind` is a closed enum, so this is exhaustive by construction
 * -- a kind added to the schema stops compiling here rather than rendering as
 * a blank row. The actor is a name where we have one and is left out of the
 * sentence entirely where we do not; "Someone commented" and "null commented"
 * are both worse than a passive sentence that is simply true.
 */
function describeNotification(
  notification: InboxNotification,
  actor: WorkspaceMember | undefined,
): string {
  const who = actor === undefined ? null : memberLabel(actor)

  switch (notification.kind) {
    case 'ASSIGNED':
      return who === null ? 'An issue was assigned to you' : `${who} assigned an issue to you`
    case 'COMMENTED':
      return who === null ? 'A new comment on an issue' : `${who} commented on an issue`
    case 'BLOCKED':
      return who === null
        ? 'An issue you follow is now blocked'
        : `${who} marked an issue you follow as blocked`
  }
}

/**
 * The viewer's notifications.
 *
 * ## What a row can and cannot say
 *
 * `Notification` carries `issueId` and nothing else about the issue -- there
 * is no `Notification.issue`, no identifier, no title. So a row links to the
 * issue it is about, which is what the inbox is for, and names the *event*
 * rather than the issue, which is all the API gives it. Resolving 25 issue
 * ids would be 25 requests per page for two strings, and matching them
 * against a separately-loaded page of issues would name some rows and not
 * others -- a list that sometimes shows a title reads as broken more than one
 * that never does.
 *
 * `actorId` is the same shape of problem with a solution: `workspaceMembers`
 * is one request for the whole screen and already loaded by
 * `useWorkspaceContext`, so a name is cheap where a title is not.
 *
 * ## Read state and the badge in the rail
 *
 * Marking read is the one thing on this screen that changes something
 * elsewhere. `useInbox` writes the unread count into the cache entry the
 * sidebar is already watching, so the badge moves in the same frame as the
 * row -- see ./api.ts. Nothing here refetches the rail.
 *
 * `AppLayout` owns the `<main>` landmark and `PageHeader` owns the page's
 * only `<h1>`, so this renders a fragment and adds neither.
 */
export function InboxPage() {
  const paths = useAppPaths()
  const [filter, setFilter] = useState<Filter>('all')

  const {
    notifications,
    unreadCount,
    hasNextPage,
    isLoadingFirstPage,
    isLoadingMore,
    isRefreshing,
    errorMessage,
    loadMoreErrorMessage,
    actionErrorMessage,
    actionStatus,
    isMarking,
    loadMore,
    retry,
    markRead,
    markAllRead,
  } = useInbox(filter === 'unread')

  // For resolving `actorId`. A failure costs names and nothing else, so it is
  // not raised to the page.
  const { memberById } = useWorkspaceContext()

  const hasRows = notifications.length > 0
  const showError = !isLoadingFirstPage && errorMessage !== null && !hasRows

  return (
    <>
      <PageHeader
        title="Inbox"
        description="Notifications about the issues you are following."
        actions={
          <div className={styles.rowActions}>
            <SegmentedControl
              label="Filter notifications"
              options={FILTERS}
              value={filter}
              onChange={setFilter}
            />

            <Button
              onClick={markAllRead}
              disabled={unreadCount === 0 || isMarking}
              loading={isMarking}
            >
              Mark all read
            </Button>
          </div>
        }
      />

      <PageContent>
        <div className={styles.stack}>
          {/*
            One live region for both outcomes of an action, so a screen-reader
            user hears the result of pressing a button that changes nothing
            visible where they are looking. `role="status"` and not `alert`
            for the success half: it is a confirmation, not an interruption.
          */}
          {actionErrorMessage !== null && (
            <p className={styles.formError} role="alert">
              {actionErrorMessage}
            </p>
          )}
          <div role="status">
            <VisuallyHidden as="div">{actionStatus ?? ''}</VisuallyHidden>
          </div>

          <p className={styles.footnote} role="status">
            {unreadCount === 0
              ? 'Nothing unread.'
              : `${unreadCount} unread ${unreadCount === 1 ? 'notification' : 'notifications'}.`}
          </p>

          {isLoadingFirstPage && (
            <div className={styles.skeletonStack} role="status" aria-busy="true">
              {/* `Skeleton` is `aria-hidden` by design, so the announcement
                  belongs on one live region for the whole list. */}
              <VisuallyHidden as="div">Loading notifications</VisuallyHidden>
              {Array.from({ length: SKELETON_ROWS }, (_unused, index) => (
                <Skeleton key={index} width="100%" height="2.25rem" />
              ))}
            </div>
          )}

          {/* The whole-list error panel replaces the list only when there is
              no list to replace. */}
          {showError && errorMessage !== null && (
            <ErrorState
              title="Could not load notifications"
              description={errorMessage}
              onRetry={isRefreshing ? undefined : retry}
            />
          )}

          {!isLoadingFirstPage && !showError && !hasRows && (
            <EmptyState
              icon={<InboxIcon />}
              title={filter === 'unread' ? 'Nothing unread' : 'No notifications yet'}
              description={
                filter === 'unread'
                  ? 'Everything here has been read. Switch to All to see the rest.'
                  : 'Vector notifies you when an issue is assigned to you, commented on, or blocked.'
              }
            />
          )}

          {hasRows && (
            <>
              <List label="Notifications">
                {notifications.map((notification) => {
                  const actor =
                    notification.actorId === null
                      ? undefined
                      : memberById.get(notification.actorId)
                  const unread = notification.readAt === null
                  const sentence = describeNotification(notification, actor)

                  return (
                    <ListRow interactive key={notification.id}>
                      <ListRowMain>
                        {/*
                          A real `<a>` stretched over the row, not a
                          `<div onClick>`: Tab reaches it, Enter follows it,
                          and "open in new tab" works, none of which a click
                          handler on a div gives for free.
                        */}
                        <Link
                          className={styles.rowLink}
                          to={paths.issue(notification.issueId)}
                        >
                          {sentence}
                          {/* The link's name has to say where it goes. The
                              issue's own identifier is not available here --
                              see the note on this component. */}
                          <VisuallyHidden> -- open the issue</VisuallyHidden>
                        </Link>
                      </ListRowMain>

                      <ListRowMeta>
                        {unread && <Badge tone="info">Unread</Badge>}

                        <time
                          className={styles.rowSub}
                          dateTime={notification.createdAt}
                          title={notification.createdAt}
                        >
                          {formatRelative(notification.createdAt)}
                        </time>

                        {unread && (
                          <span className={styles.rowActions}>
                            <Button
                              size="sm"
                              disabled={isMarking}
                              onClick={() => {
                                markRead(notification.id)
                              }}
                            >
                              Mark read
                              {/* Every row's button would otherwise be named
                                  "Mark read", which is useless in a list of
                                  buttons read out one after another. */}
                              <VisuallyHidden>: {sentence}</VisuallyHidden>
                            </Button>
                          </span>
                        )}
                      </ListRowMeta>
                    </ListRow>
                  )
                })}
              </List>

              <ListFooter
                noun="notification"
                loadedCount={notifications.length}
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
