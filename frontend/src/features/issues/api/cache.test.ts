import { describe, expect, it } from 'vitest'

import { createTestClient } from '../../../test/client'
import { ControlledLink } from '../../../test/controlledLink'
import {
  cursor,
  issueDetail,
  issueListData,
  issueRowRange,
} from '../../../test/factories'
import { IssueListDocument, prependCreatedIssue } from './index'

/**
 * `prependCreatedIssue`, and the alternative it exists instead of.
 *
 * The second half of this file is the more important half. `prependCreatedIssue`
 * looks like ceremony next to a one-line `refetch()` after the mutation, and
 * the next person to read `useCreateIssue` will think so too. The reason it is
 * not is measured below: the merge policy treats a request with no cursor as a
 * request for the *start* of the list and replaces rather than extends, so a
 * `refetch()` -- whose variables are `after: null` -- throws away every page
 * the user loaded past the first.
 *
 * A user who clicked "Load more" four times and then created an issue would
 * watch 100 rows collapse to 25. That is the regression this file exists to
 * make fail loudly rather than silently.
 */

const PAGE_ONE_END = cursor('page-one')
const PAGE_TWO_END = cursor('page-two')

/**
 * One emission from a live subscription.
 *
 * `null` means the emission carried no complete data. Apollo v4 discriminates
 * its result on `dataState`, and treating an `empty` emission as an empty list
 * would let a lost result pass an assertion about a legitimately empty one.
 */
type Emission =
  | { complete: false }
  | {
      complete: true
      ids: string[]
      endCursor: string | null
      hasNextPage: boolean
    }

/**
 * A live query over a controlled link, at real page sizes.
 *
 * Assertions read the subscription's emissions rather than
 * `observable.getCurrentResult()`: an `ObservableQuery` with no live
 * subscriber does not keep its result current, and polling it measures
 * something other than what the screen would show.
 */
function openList() {
  const link = new ControlledLink()
  const client = createTestClient(link)

  const observable = client.watchQuery({
    query: IssueListDocument,
    variables: { after: null },
    notifyOnNetworkStatusChange: true,
  })

  const emissions: Emission[] = []

  const subscription = observable.subscribe((result) => {
    if (result.dataState !== 'complete') {
      emissions.push({ complete: false })
      return
    }

    const { nodes, pageInfo } = result.data.issues

    emissions.push({
      complete: true,
      ids: nodes.map((node) => node.id),
      endCursor: pageInfo.endCursor,
      hasNextPage: pageInfo.hasNextPage,
    })
  })

  return {
    link,
    client,
    observable,
    emissions,
    close: () => {
      subscription.unsubscribe()
    },
  }
}

/** The most recent emission that carried a complete list. */
function latest(
  emissions: readonly Emission[],
): Extract<Emission, { complete: true }> {
  const last = emissions.at(-1)

  if (last === undefined) {
    throw new Error('The query has not emitted anything yet')
  }

  if (!last.complete) {
    throw new Error('The latest emission carried no complete data')
  }

  return last
}

/** Two full pages loaded, the way a user who pressed "Load more" once has. */
async function loadTwoPages(probe: ReturnType<typeof openList>): Promise<void> {
  await probe.link.resolve('IssueList', {
    data: issueListData(issueRowRange(1, 25), {
      hasNextPage: true,
      endCursor: PAGE_ONE_END,
    }),
  })

  const pending = probe.observable.fetchMore({ variables: { after: PAGE_ONE_END } })

  await probe.link.resolve('IssueList', {
    data: issueListData(issueRowRange(26, 25), {
      hasNextPage: true,
      endCursor: PAGE_TWO_END,
    }),
  })

  await pending
}

describe('prependCreatedIssue', () => {
  it('adds exactly one row at the head and keeps every loaded page', async () => {
    const probe = openList()

    try {
      await loadTwoPages(probe)
      expect(latest(probe.emissions).ids).toHaveLength(50)

      const created = issueDetail(999, { title: 'Just created' })
      prependCreatedIssue(probe.client.cache, created)
      await probe.link.idle()

      const after = latest(probe.emissions)

      // Grew by exactly one, at the head -- which is where the server's
      // `created_at DESC, id DESC` ordering would have put it.
      expect(after.ids).toHaveLength(51)
      expect(after.ids[0]).toBe(created.id)
      expect(new Set(after.ids).size).toBe(51)

      /*
        And `pageInfo` is untouched. That is correct *because the pagination
        is keyset and not offset*: `endCursor` encodes the position of the
        last row fetched -- the tail frontier -- and inserting at the head
        does not move the tail. Under OFFSET pagination the same insertion
        would shift every following page by one and duplicate a row at each
        boundary.
      */
      expect(after.endCursor).toBe(PAGE_TWO_END)
      expect(after.hasNextPage).toBe(true)

      // No request was made. The row arrived by a cache write.
      expect(probe.link.countOf('IssueList')).toBe(2)
    } finally {
      probe.close()
    }
  })

  it('does not duplicate when applied twice for one issue', async () => {
    const probe = openList()

    try {
      await loadTwoPages(probe)

      const created = issueDetail(999, { title: 'Just created' })

      // A retried update, or a future optimistic response reconciled against
      // the real one.
      prependCreatedIssue(probe.client.cache, created)
      prependCreatedIssue(probe.client.cache, created)
      await probe.link.idle()

      const after = latest(probe.emissions)

      expect(after.ids).toHaveLength(51)
      expect(new Set(after.ids).size).toBe(51)
      expect(after.endCursor).toBe(PAGE_TWO_END)
    } finally {
      probe.close()
    }
  })

  it('does nothing when the list has never been read', () => {
    const link = new ControlledLink()
    const client = createTestClient(link)

    // No list on screen to keep consistent. Writing one would invent a first
    // page out of a single row and then report `hasNextPage: false` about it.
    prependCreatedIssue(client.cache, issueDetail(999))

    expect(
      client.cache.readQuery({ query: IssueListDocument, variables: { after: null } }),
    ).toBeNull()
  })
})

describe('why the create flow does not refetch', () => {
  it('truncates 50 loaded rows back to 25', async () => {
    const probe = openList()

    try {
      await loadTwoPages(probe)
      expect(latest(probe.emissions).ids).toHaveLength(50)

      /*
        ===============================================================
        READ THIS BEFORE REPLACING THE CACHE UPDATE WITH A REFETCH
        ===============================================================

        This test is not describing a bug. It is pinning the cost of the
        obvious simplification, so that making it fails here rather than in
        front of a user.

        `refetch()` re-runs the query with its original variables, which are
        `after: null`. The field policy in `src/lib/graphql/cache.ts` reads a
        null cursor as "start the list over" and replaces the accumulated
        list with the incoming page -- a branch that is correct and necessary,
        because it is what stops a re-fetched page one from being appended to
        itself.

        So a `refetch()` after creating an issue discards every page loaded
        past the first. Four presses of "Load more" and 100 rows would become
        25, with no error, no warning and nothing in the console.

        That is why `useCreateIssue` writes the new row into the cache by hand
        (./cache.ts) instead of refetching. If you are here because you were
        simplifying that away: this is the behaviour you would be shipping.
      */
      const refetching = probe.observable.refetch()

      await probe.link.resolve('IssueList', {
        data: issueListData(issueRowRange(1, 25), {
          hasNextPage: true,
          endCursor: PAGE_ONE_END,
        }),
      })
      await refetching
      await probe.link.idle()

      expect(latest(probe.emissions).ids).toHaveLength(25)

      // And the truncation really came from the reset branch: the request
      // that caused it carried no cursor.
      expect(probe.link.operationsNamed('IssueList').at(-1)?.variables['after']).toBeNull()
      expect(latest(probe.emissions).endCursor).toBe(PAGE_ONE_END)
    } finally {
      probe.close()
    }
  })
})
