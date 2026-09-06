import { NetworkStatus } from '@apollo/client'
import { describe, expect, it } from 'vitest'

import { IssueListDocument } from '../../features/issues/api'
import type { IssueListData } from '../../features/issues/api'
import { createTestClient } from '../../test/client'
import { ControlledLink } from '../../test/controlledLink'
import { cursor, issueListData, issueRow, issueRowRange } from '../../test/factories'
import { createCache } from './cache'

/**
 * The `issues` field policy, driven directly.
 *
 * The pagination screens are covered in
 * `src/features/issues/pagination.test.tsx`; this file exists for the parts of
 * the policy a screen cannot reach -- what `keyArgs: false` does to the store,
 * what happens when the same page is written twice through paths the UI's own
 * in-flight guard prevents, and the `networkStatus` transitions that no
 * rendered element reports directly.
 *
 * The first half writes through `cache.writeQuery`, which is the same path
 * Apollo's network results take, so the merge function under test is the real
 * one and is entered the same way. The second half drives a real
 * `ObservableQuery` over a controlled link, at real page sizes.
 */

const PAGE_ONE_END = cursor('page-one')
const PAGE_TWO_END = cursor('page-two')
const PAGE_THREE_END = cursor('page-three')

function writePage(
  cache: ReturnType<typeof createCache>,
  after: string | null,
  data: IssueListData,
): void {
  cache.writeQuery({ query: IssueListDocument, variables: { after }, data })
}

function readTitles(cache: ReturnType<typeof createCache>): string[] {
  const data = cache.readQuery({
    query: IssueListDocument,
    variables: { after: null },
  })

  return (data?.issues.nodes ?? []).map((node) => node.title)
}

describe('issues field policy', () => {
  it('appends a page fetched with a cursor', () => {
    const cache = createCache()

    writePage(
      cache,
      null,
      issueListData([issueRow(1, { title: 'Alpha' }), issueRow(2, { title: 'Bravo' })], {
        hasNextPage: true,
        endCursor: PAGE_ONE_END,
      }),
    )

    writePage(
      cache,
      PAGE_ONE_END,
      issueListData([issueRow(3, { title: 'Charlie' })], {
        hasNextPage: false,
        endCursor: PAGE_TWO_END,
      }),
    )

    expect(readTitles(cache)).toEqual(['Alpha', 'Bravo', 'Charlie'])
  })

  it('keeps every page in one cache field regardless of arguments', () => {
    const cache = createCache()

    writePage(
      cache,
      null,
      issueListData([issueRow(1)], { hasNextPage: true, endCursor: PAGE_ONE_END }),
    )
    writePage(
      cache,
      PAGE_ONE_END,
      issueListData([issueRow(2)], { hasNextPage: false, endCursor: PAGE_TWO_END }),
    )

    const root = cache.extract()['ROOT_QUERY'] ?? {}
    const issueFields = Object.keys(root).filter((key) => key.startsWith('issues'))

    /*
      One field, keyed on the name alone. This is what `keyArgs: false` buys,
      and the failure it prevents is silent: with argument-keyed fields,
      `issues({"after":null})` and `issues({"after":"..."})` become unrelated
      entries, `fetchMore` writes page two somewhere no active query is
      watching, and the list never grows no matter how many times the button
      is pressed. Apollo reports none of that.
    */
    expect(issueFields).toEqual(['issues'])
  })

  it('drops a node the incoming page repeats', () => {
    const cache = createCache()

    writePage(
      cache,
      null,
      issueListData([issueRow(1, { title: 'Alpha' }), issueRow(2, { title: 'Bravo' })], {
        hasNextPage: true,
        endCursor: PAGE_ONE_END,
      }),
    )

    writePage(
      cache,
      PAGE_ONE_END,
      issueListData([issueRow(2, { title: 'Bravo' }), issueRow(3, { title: 'Charlie' })], {
        hasNextPage: false,
        endCursor: PAGE_TWO_END,
      }),
    )

    expect(readTitles(cache)).toEqual(['Alpha', 'Bravo', 'Charlie'])
  })

  it('drops the whole page when the same cursor is written twice', () => {
    const cache = createCache()

    writePage(
      cache,
      null,
      issueListData([issueRow(1, { title: 'Alpha' })], {
        hasNextPage: true,
        endCursor: PAGE_ONE_END,
      }),
    )

    const secondPage = issueListData([issueRow(2, { title: 'Bravo' })], {
      hasNextPage: false,
      endCursor: PAGE_TWO_END,
    })

    writePage(cache, PAGE_ONE_END, secondPage)
    writePage(cache, PAGE_ONE_END, secondPage)

    // Identity comes from the entity, not from array position, so replaying a
    // page adds nothing.
    expect(readTitles(cache)).toEqual(['Alpha', 'Bravo'])
  })

  it('replaces the list when a page is fetched without a cursor', () => {
    const cache = createCache()

    writePage(
      cache,
      null,
      issueListData([issueRow(1, { title: 'Alpha' })], {
        hasNextPage: true,
        endCursor: PAGE_ONE_END,
      }),
    )
    writePage(
      cache,
      PAGE_ONE_END,
      issueListData([issueRow(2, { title: 'Bravo' })], {
        hasNextPage: true,
        endCursor: PAGE_TWO_END,
      }),
    )
    expect(readTitles(cache)).toEqual(['Alpha', 'Bravo'])

    /*
      A refetch, a `cache-and-network` poll, or a StrictMode double-mount:
      all of them re-request page one with no cursor. Without the reset
      branch each one appends a second copy of the rows already on screen --
      the "load more duplicates rows" bug, surfacing far from its cause.
    */
    writePage(
      cache,
      null,
      issueListData([issueRow(1, { title: 'Alpha' })], {
        hasNextPage: true,
        endCursor: PAGE_ONE_END,
      }),
    )

    expect(readTitles(cache)).toEqual(['Alpha'])
  })

  it('takes pageInfo from the newest page and never merges it', () => {
    const cache = createCache()

    writePage(
      cache,
      null,
      issueListData([issueRow(1)], { hasNextPage: true, endCursor: PAGE_ONE_END }),
    )
    writePage(
      cache,
      PAGE_ONE_END,
      issueListData([issueRow(2)], { hasNextPage: false, endCursor: PAGE_TWO_END }),
    )

    const data = cache.readQuery({
      query: IssueListDocument,
      variables: { after: null },
    })

    /*
      The frontier is wherever the newest page ended. Keeping the older
      `pageInfo` would hand the next `fetchMore` a cursor it has already
      consumed and loop on the same page forever -- which presents as a
      "Load more" button that appears to work and changes nothing.
    */
    expect(data?.issues.pageInfo).toEqual({
      __typename: 'PageInfo',
      hasNextPage: false,
      endCursor: PAGE_TWO_END,
    })
  })
})

/**
 * One emission from a live subscription.
 *
 * Read from the subscription and never from `observable.getCurrentResult()`.
 * An `ObservableQuery` with no subscriber does not keep its result current, so
 * polling `getCurrentResult()` reads a stale value and reports rows that are
 * not there (or misses rows that are) -- which looks exactly like a UI bug and
 * is not one. A React component holds the subscription open; so does this.
 */
interface Emission {
  networkStatus: NetworkStatus
  /**
   * `null` when the emission carried no complete data.
   *
   * Distinct from `[]`, deliberately. Apollo v4 discriminates its result on
   * `dataState`, and an emission that is `empty` or `streaming` is not the
   * same event as one reporting a list with no rows -- conflating them would
   * let a fetchMore that dropped its data pass an assertion about a list that
   * was legitimately empty.
   */
  ids: string[] | null
}

function openList() {
  const link = new ControlledLink()
  const client = createTestClient(link)

  const observable = client.watchQuery({
    query: IssueListDocument,
    variables: { after: null },
    // Exactly what `useIssueList` sets, and for the same reason: the whole
    // loading model depends on the status changing mid-flight.
    notifyOnNetworkStatusChange: true,
  })

  const emissions: Emission[] = []

  const subscription = observable.subscribe((result) => {
    if (result.dataState !== 'complete') {
      emissions.push({ networkStatus: result.networkStatus, ids: null })
      return
    }

    emissions.push({
      networkStatus: result.networkStatus,
      ids: result.data.issues.nodes.map((node) => node.id),
    })
  })

  return {
    link,
    observable,
    emissions,
    close: () => {
      subscription.unsubscribe()
    },
  }
}

/** The ids the most recent complete emission carried. */
function currentIds(emissions: readonly Emission[]): string[] {
  const last = emissions.at(-1)

  if (last === undefined) {
    throw new Error('The query has not emitted anything yet')
  }

  if (last.ids === null) {
    throw new Error(
      `The latest emission (networkStatus ${last.networkStatus}) carried no complete data`,
    )
  }

  return last.ids
}

function latestStatus(emissions: readonly Emission[]): NetworkStatus {
  const last = emissions.at(-1)

  if (last === undefined) {
    throw new Error('The query has not emitted anything yet')
  }

  return last.networkStatus
}

function expectUnique(ids: readonly string[]): void {
  expect(new Set(ids).size).toBe(ids.length)
}

/** Ask for the next page and answer it. */
async function loadPage(
  probe: ReturnType<typeof openList>,
  after: string,
  data: IssueListData,
): Promise<void> {
  const pending = probe.observable.fetchMore({ variables: { after } })

  await probe.link.resolve('IssueList', { data })
  await pending
}

describe('issues field policy, through a live query', () => {
  it('accumulates three pages with every id unique, asking for each by cursor', async () => {
    const probe = openList()

    try {
      await probe.link.resolve('IssueList', {
        data: issueListData(issueRowRange(1, 25), {
          hasNextPage: true,
          endCursor: PAGE_ONE_END,
        }),
      })

      expect(currentIds(probe.emissions)).toHaveLength(25)
      expectUnique(currentIds(probe.emissions))

      await loadPage(
        probe,
        PAGE_ONE_END,
        issueListData(issueRowRange(26, 25), {
          hasNextPage: true,
          endCursor: PAGE_TWO_END,
        }),
      )

      expect(currentIds(probe.emissions)).toHaveLength(50)
      expectUnique(currentIds(probe.emissions))

      await loadPage(
        probe,
        PAGE_TWO_END,
        issueListData(issueRowRange(51, 5), {
          hasNextPage: false,
          endCursor: PAGE_THREE_END,
        }),
      )

      expect(currentIds(probe.emissions)).toHaveLength(55)
      expectUnique(currentIds(probe.emissions))

      const sent = probe.link.operationsNamed('IssueList')

      // Cursors, in order, and nothing else. An offset-based implementation
      // would have sent numbers here, and a broken `keyArgs` would have sent
      // the same cursor repeatedly.
      expect(sent.map((operation) => operation.variables['after'])).toEqual([
        null,
        PAGE_ONE_END,
        PAGE_TWO_END,
      ])

      // `first` is a literal in the document, so it must never travel as a
      // variable -- as one it is charged at 100 against the 1000 complexity
      // budget instead of at 25.
      expect(
        sent.every((operation) => operation.variables['first'] === undefined),
      ).toBe(true)
    } finally {
      probe.close()
    }
  })

  it('does not grow the list when the same cursor is fetched twice', async () => {
    const probe = openList()

    try {
      await probe.link.resolve('IssueList', {
        data: issueListData(issueRowRange(1, 25), {
          hasNextPage: true,
          endCursor: PAGE_ONE_END,
        }),
      })

      const secondPage = issueListData(issueRowRange(26, 25), {
        hasNextPage: true,
        endCursor: PAGE_TWO_END,
      })

      await loadPage(probe, PAGE_ONE_END, secondPage)
      expect(currentIds(probe.emissions)).toHaveLength(50)

      // The same cursor again -- a double-clicked "Load more" that got past
      // the screen's in-flight guard, or a retry. The policy's dedupe is what
      // keeps this from producing 75 rows with 25 of them repeated.
      await loadPage(probe, PAGE_ONE_END, secondPage)

      expect(currentIds(probe.emissions)).toHaveLength(50)
      expectUnique(currentIds(probe.emissions))
      expect(probe.link.countOf('IssueList')).toBe(3)
    } finally {
      probe.close()
    }
  })

  it('reports a fetchMore in flight with the previous page still present', async () => {
    const probe = openList()

    try {
      await probe.link.resolve('IssueList', {
        data: issueListData(issueRowRange(1, 25), {
          hasNextPage: true,
          endCursor: PAGE_ONE_END,
        }),
      })

      const pending = probe.observable.fetchMore({ variables: { after: PAGE_ONE_END } })
      await probe.link.waitForRequest('IssueList')

      /*
        `NetworkStatus.fetchMore` is 3, and it is the status the screen turns
        into "Loading more issues..." rather than into a skeleton. What makes
        it worth asserting here as well as in the rendered output is the
        second half of the claim: every in-flight emission still carries all
        25 rows. A status that arrived with an empty `data` would blank the
        list mid-load, and no amount of care in the component could put the
        rows back.
      */
      const inFlight = probe.emissions.filter(
        (emission) => emission.networkStatus === NetworkStatus.fetchMore,
      )

      expect(inFlight.length).toBeGreaterThan(0)
      expect(inFlight.every((emission) => emission.ids?.length === 25)).toBe(true)

      await probe.link.resolve('IssueList', {
        data: issueListData(issueRowRange(26, 25), {
          hasNextPage: false,
          endCursor: PAGE_TWO_END,
        }),
      })
      await pending

      expect(latestStatus(probe.emissions)).toBe(NetworkStatus.ready)
      expect(currentIds(probe.emissions)).toHaveLength(50)
    } finally {
      probe.close()
    }
  })
})
