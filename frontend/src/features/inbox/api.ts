import { useCallback, useState } from 'react'
import { NetworkStatus } from '@apollo/client'
import { useMutation, useQuery } from '@apollo/client/react'

import { useWorkspaceSlug } from '../../app/routes'
import { describeError } from '../screens'
import {
  NotificationInboxDocument,
  NotificationMarkAllReadDocument,
  NotificationMarkReadDocument,
  NotificationUnreadCountDocument,
} from '../../generated/operations'
import type { NotificationFieldsFragment } from '../../generated/operations'

/**
 * The inbox's data adapter.
 *
 * The boundary the screen is written against: it imports `useInbox` and
 * `InboxNotification` and never a document, an Apollo hook, or an Apollo
 * error type. The documents live in ./operations.graphql and are re-exported
 * below only because mocking a response in a test requires the exact
 * document that produced it.
 */

export {
  NotificationInboxDocument,
  NotificationMarkAllReadDocument,
  NotificationMarkReadDocument,
  NotificationUnreadCountDocument,
} from '../../generated/operations'

/** One notification, exactly as the inbox selects it. */
export type InboxNotification = NotificationFieldsFragment

/** Stable identity for "nothing yet", so a first render does not churn memos. */
const NO_NOTIFICATIONS: readonly InboxNotification[] = []

export interface UseInboxResult {
  notifications: readonly InboxNotification[]
  /** Unread across the whole workspace, not merely among the rows loaded. */
  unreadCount: number
  hasNextPage: boolean
  /** The very first fetch, with nothing on screen yet. */
  isLoadingFirstPage: boolean
  /** A `fetchMore`, with rows already on screen. Deliberately separate. */
  isLoadingMore: boolean
  isRefreshing: boolean
  /** A failure that concerns the list as a whole. */
  errorMessage: string | null
  /** A failure of the most recent "load more", which leaves rows intact. */
  loadMoreErrorMessage: string | null
  /** A failure of marking something read. Belongs to no row's own state. */
  actionErrorMessage: string | null
  /** What the last successful mark-all-read did, for the live region. */
  actionStatus: string | null
  isMarking: boolean
  loadMore: () => void
  retry: () => void
  markRead: (id: string) => void
  markAllRead: () => void
}

/**
 * The viewer's notifications, and the two ways to mark them read.
 *
 * ## Pages accumulate in the cache, not here
 *
 * `src/lib/graphql/cache.ts` owns the field policy that merges pages of
 * `notifications`, keyed on `workspaceSlug` *and* `unreadOnly` -- the two
 * arguments that select a different list. Without a policy, `fetchMore`
 * writes page two under a cache key that no active query watches and "Load
 * more" becomes a button that sends a request and changes nothing; Apollo
 * does not report that as an error. So `fetchMore` is called for its effect
 * on the cache and its resolved value is deliberately ignored.
 *
 * ## Why the badge in the rail updates on its own
 *
 * A cache field is keyed by its name and its arguments, not by the operation
 * that fetched it. `ShellSidebar` selects
 * `notificationUnreadCount(workspaceSlug:)` and so does this hook, so both
 * read one entry -- and writing it here moves the rail's badge with no
 * refetch of the rail and no import in either direction.
 *
 * Mark-one is written by hand rather than refetched: `notificationMarkRead`
 * returns the notification, which Apollo normalises so the row's `readAt`
 * updates by itself, but a count is a scalar with no entity to normalise
 * over. Decrementing is exact here because the hook refuses to send the
 * mutation at all for a notification that is already read.
 *
 * Mark-all is different in kind: the payload carries only a count, so
 * nothing normalises and every `readAt` on screen is stale. That one sets
 * the count to zero -- which is what the server has just made true -- and
 * re-reads the list.
 */
export function useInbox(unreadOnly: boolean): UseInboxResult {
  const workspaceSlug = useWorkspaceSlug()

  const [loadMoreErrorMessage, setLoadMoreErrorMessage] = useState<string | null>(null)
  const [actionErrorMessage, setActionErrorMessage] = useState<string | null>(null)
  const [actionStatus, setActionStatus] = useState<string | null>(null)

  const { data, error, networkStatus, fetchMore, refetch } = useQuery(
    NotificationInboxDocument,
    {
      // `after: null` stated rather than omitted: the merge policy reads
      // `args.after` to decide whether a result starts the list or extends
      // it, and that decision should read a value the document declares.
      variables: { workspaceSlug, unreadOnly, after: null },
      notifyOnNetworkStatusChange: true,
    },
  )

  // Its own request, and a cheap one -- a single scalar, charged once by the
  // complexity rule because the field takes no page size. A failure costs
  // the count and nothing else, so it is not raised to the page.
  const { data: countData } = useQuery(NotificationUnreadCountDocument, {
    variables: { workspaceSlug },
  })

  const [markRead, markReadState] = useMutation(NotificationMarkReadDocument)
  const [markAll, markAllState] = useMutation(NotificationMarkAllReadDocument)

  const connection = data?.notifications
  const notifications = connection?.nodes ?? NO_NOTIFICATIONS
  const endCursor = connection?.pageInfo.endCursor ?? null
  const hasNextPage = connection?.pageInfo.hasNextPage ?? false
  const isLoadingMore = networkStatus === NetworkStatus.fetchMore

  const loadMore = useCallback(() => {
    // `hasNextPage` false is the end of the connection, and a null cursor on
    // a non-empty page would mean the server had no position to resume from.
    // Asking anyway would send `after: null`, which the merge policy reads as
    // "start the list over" and which would wipe every page already loaded.
    if (!hasNextPage || endCursor === null || isLoadingMore) {
      return
    }

    setLoadMoreErrorMessage(null)

    void fetchMore({ variables: { after: endCursor } }).catch((reason: unknown) => {
      // Caught rather than left to reject: a failed "load more" must not
      // clear the rows already on screen, and an unhandled rejection would be
      // reported as a page-level crash by any error tracker.
      setLoadMoreErrorMessage(describeError(reason))
    })
  }, [endCursor, fetchMore, hasNextPage, isLoadingMore])

  const retry = useCallback(() => {
    setLoadMoreErrorMessage(null)
    // Swallowed on purpose: `refetch` rejects *and* sets `error` on the hook
    // result, and `error` is what the screen renders.
    void refetch().catch(() => undefined)
  }, [refetch])

  const handleMarkRead = useCallback(
    (id: string) => {
      const target = notifications.find((notification) => notification.id === id)

      // Already read, or not on screen: no request. This is also what makes
      // the decrement below exact rather than a guess -- the count can only
      // move for a notification that was genuinely counted in it.
      if (target === undefined || target.readAt !== null) {
        return
      }

      setActionErrorMessage(null)
      setActionStatus(null)

      void markRead({
        variables: { input: { workspaceSlug, id } },

        update(cache, result) {
          if (result.data?.notificationMarkRead.notification == null) {
            return
          }

          cache.updateQuery(
            { query: NotificationUnreadCountDocument, variables: { workspaceSlug } },
            (current) =>
              current === null
                ? current
                : {
                    ...current,
                    // Floored at zero. The count and the list are two
                    // answers from two requests and can disagree for a
                    // moment; a negative badge is the one way that
                    // disagreement must never be rendered.
                    notificationUnreadCount: Math.max(
                      0,
                      current.notificationUnreadCount - 1,
                    ),
                  },
          )
        },
      })
        .then((result) => {
          const rejected = result.data?.notificationMarkRead.errors ?? []

          if (rejected.length > 0) {
            setActionErrorMessage(rejected.map((entry) => entry.message).join(' '))
          }
        })
        .catch((reason: unknown) => {
          setActionErrorMessage(describeError(reason))
        })
    },
    [markRead, notifications, workspaceSlug],
  )

  const handleMarkAllRead = useCallback(() => {
    setActionErrorMessage(null)
    setActionStatus(null)

    void markAll({
      variables: { input: { workspaceSlug } },

      update(cache, result) {
        if (result.data === undefined || result.data === null) {
          return
        }

        // Zero rather than "minus markedCount": the server has just marked
        // everything unread in this workspace, so zero is the truth and not
        // an optimistic guess.
        cache.updateQuery(
          { query: NotificationUnreadCountDocument, variables: { workspaceSlug } },
          () => ({ notificationUnreadCount: 0 }),
        )
      },
    })
      .then((result) => {
        const payload = result.data?.notificationMarkAllRead

        if (payload === undefined || payload === null) {
          return
        }

        if (payload.errors.length > 0) {
          setActionErrorMessage(payload.errors.map((entry) => entry.message).join(' '))
          return
        }

        setActionStatus(
          payload.markedCount === 0
            ? 'Nothing was unread.'
            : `Marked ${payload.markedCount} ${payload.markedCount === 1 ? 'notification' : 'notifications'} read.`,
        )

        // The payload carries no notifications, so every `readAt` on screen
        // is now stale and nothing normalised it away. Re-read rather than
        // rewrite: a client-invented timestamp would be a small lie in the
        // cache, and under `unreadOnly` the correct new list is empty.
        return refetch().then(() => undefined)
      })
      .catch((reason: unknown) => {
        setActionErrorMessage(describeError(reason))
      })
  }, [markAll, refetch, workspaceSlug])

  return {
    notifications,
    unreadCount: countData?.notificationUnreadCount ?? 0,
    hasNextPage,
    isLoadingFirstPage: networkStatus === NetworkStatus.loading,
    isLoadingMore,
    isRefreshing: networkStatus === NetworkStatus.refetch,
    errorMessage: error === undefined ? null : describeError(error),
    loadMoreErrorMessage,
    actionErrorMessage,
    actionStatus,
    isMarking: markReadState.loading || markAllState.loading,
    loadMore,
    retry,
    markRead: handleMarkRead,
    markAllRead: handleMarkAllRead,
  }
}
