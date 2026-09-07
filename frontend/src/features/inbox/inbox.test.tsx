import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { main, renderApp } from '../../test/render'
import {
  MEMBER_ID,
  WORKSPACE_SLUG,
  cursor,
  shellSidebarData,
  workspaceContextData,
} from '../../test/factories'
import type {
  NotificationInboxQuery,
  NotificationMarkAllReadMutation,
  NotificationMarkReadMutation,
} from '../../generated/operations'

/**
 * The inbox, against the real router, the real cache and a controlled network.
 *
 * Two things here can break silently and neither shows up in a screenshot:
 *
 *   1. The **badge** in the rail and the read state on the row are two
 *      renderings of one fact. They agree only because both read the same
 *      cache entry, and nothing but a test notices when they stop.
 *   2. The **field policy** keys `notifications` on `unreadOnly`. Without
 *      that key, switching the filter merges one list into the other and read
 *      rows never leave the unread view -- and Apollo reports nothing.
 */

const PATH = `/${WORKSPACE_SLUG}/inbox`
const ISSUE_ID = '00000000-0000-4000-8000-0000000000d1'
const NOTIFICATION_ID = '00000000-0000-4000-8000-0000000000e1'

function notification(
  overrides: Partial<NotificationInboxQuery['notifications']['nodes'][number]> = {},
): NotificationInboxQuery['notifications']['nodes'][number] {
  return {
    __typename: 'Notification',
    id: NOTIFICATION_ID,
    actorId: MEMBER_ID,
    issueId: ISSUE_ID,
    kind: 'ASSIGNED',
    readAt: null,
    createdAt: '2026-01-01T00:00:00.000Z',
    ...overrides,
  }
}

function inboxData(
  nodes: readonly NotificationInboxQuery['notifications']['nodes'][number][],
  hasNextPage = false,
): NotificationInboxQuery {
  return {
    notifications: {
      __typename: 'NotificationConnection',
      nodes: [...nodes],
      pageInfo: {
        __typename: 'PageInfo',
        hasNextPage,
        endCursor: hasNextPage ? cursor('inbox-1') : null,
      },
    },
  }
}

/**
 * Mount the inbox with the rail's badge already showing a count.
 *
 * The sidebar is answered on purpose here, though most tests in this suite
 * leave it unanswered: its `notificationUnreadCount` is the badge under test,
 * and answering it also writes the cache entry the page then reads -- which
 * is exactly the sharing this file exists to prove.
 */
async function openInbox(unread: number, nodes: readonly ReturnType<typeof notification>[]) {
  const view = renderApp({
    initialPath: PATH,
    sidebar: shellSidebarData([], unread),
  })

  await view.link.resolve('NotificationInbox', { data: inboxData(nodes) })
  await view.link.resolve('IssueWorkspaceContext', { data: workspaceContextData() })

  return view
}

describe('the inbox', () => {
  it('asks for the workspace in the URL, unfiltered to begin with', async () => {
    const view = renderApp({ initialPath: PATH })

    await expect(view.link.waitForRequest('NotificationInbox')).resolves.toMatchObject({
      workspaceSlug: WORKSPACE_SLUG,
      unreadOnly: false,
      after: null,
    })
  })

  it('reads the unread count out of the same cache entry the rail wrote', async () => {
    const view = await openInbox(2, [notification()])

    // The page never asked for the count: `ShellSidebar` had already put it in
    // the cache under `notificationUnreadCount({workspaceSlug})`, and the
    // page's own document reads that same key. If these ever became two
    // entries, this request would appear and the badge would drift.
    expect(view.link.countOf('NotificationUnreadCount')).toBe(0)
    expect(within(main()).getByText('2 unread notifications.')).toBeInTheDocument()
  })

  it('moves the rail’s badge when a notification is marked read', async () => {
    const view = await openInbox(2, [notification()])

    const rail = screen.getByRole('navigation', { name: 'Main' })
    expect(within(rail).getByRole('link', { name: /Inbox/ })).toHaveAccessibleName(
      'Inbox, 2 unread',
    )

    await view.user.click(screen.getByRole('button', { name: /^Mark read/ }))

    const marked: NotificationMarkReadMutation = {
      notificationMarkRead: {
        __typename: 'NotificationMarkReadPayload',
        notification: notification({ readAt: '2026-01-02T00:00:00.000Z' }),
        errors: [],
      },
    }

    await view.link.resolve('NotificationMarkRead', { data: marked })

    // The badge, the page's own sentence and the row all moved, and only one
    // of the three was written: the mutation normalises the row, and the count
    // was decremented in the entry both readers share.
    expect(within(rail).getByRole('link', { name: /Inbox/ })).toHaveAccessibleName(
      'Inbox, 1 unread',
    )
    expect(within(main()).getByText('1 unread notification.')).toBeInTheDocument()
    expect(
      within(main()).queryByRole('button', { name: /^Mark read/ }),
    ).not.toBeInTheDocument()
  })

  it('does not send a request for a notification that is already read', async () => {
    const view = await openInbox(0, [
      notification({ readAt: '2026-01-02T00:00:00.000Z' }),
    ])

    // No button at all for a read row, which is also what makes the
    // decrement above exact rather than a guess.
    expect(
      within(main()).queryByRole('button', { name: /^Mark read/ }),
    ).not.toBeInTheDocument()
    expect(view.link.countOf('NotificationMarkRead')).toBe(0)
  })

  it('empties the badge and re-reads the list after marking everything read', async () => {
    const view = await openInbox(2, [notification()])

    await view.user.click(screen.getByRole('button', { name: 'Mark all read' }))

    const marked: NotificationMarkAllReadMutation = {
      notificationMarkAllRead: {
        __typename: 'NotificationMarkAllReadPayload',
        markedCount: 2,
        errors: [],
      },
    }

    await view.link.resolve('NotificationMarkAllRead', { data: marked })

    // The payload carries no notifications, so nothing normalised and every
    // `readAt` on screen is stale -- the list has to be re-read rather than
    // rewritten with a client-invented timestamp.
    await view.link.resolve('NotificationInbox', {
      data: inboxData([notification({ readAt: '2026-01-02T00:00:00.000Z' })]),
    })

    const rail = screen.getByRole('navigation', { name: 'Main' })
    expect(within(rail).getByRole('link', { name: /Inbox/ })).toHaveAccessibleName('Inbox')
    expect(within(main()).getByText('Nothing unread.')).toBeInTheDocument()
  })

  it('asks for a different list when the unread filter is switched on', async () => {
    const view = await openInbox(1, [notification()])

    await view.user.click(screen.getByRole('radio', { name: 'Unread' }))

    // A second request rather than a filtered re-read of the first, and the
    // cache keeps them apart on `unreadOnly` -- otherwise page two of one
    // would merge into the other.
    await expect(view.link.waitForRequest('NotificationInbox')).resolves.toMatchObject({
      unreadOnly: true,
      after: null,
    })
  })

  it('links every row to the issue it is about', async () => {
    const view = await openInbox(1, [notification()])

    const list = within(main()).getByRole('list', { name: 'Notifications' })
    const link = within(list).getByRole('link')

    expect(link).toHaveAttribute('href', `/${WORKSPACE_SLUG}/issues/${ISSUE_ID}`)
    // The actor resolves through `workspaceMembers`; the issue cannot resolve
    // at all, because `Notification` carries no reference to it beyond an id.
    // The comma rather than a space: an accessible name concatenates each
    // node's *trimmed* text, so only punctuation survives as a separator.
    expect(link).toHaveAccessibleName(
      'Ada Lovelace assigned an issue to you, open the issue',
    )
    expect(view.link.countOf('IssueDetail')).toBe(0)
  })
})
