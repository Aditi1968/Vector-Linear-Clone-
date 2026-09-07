import { describe, expect, it } from 'vitest'

import {
  DEFAULT_VIEW,
  NONE,
  applyBoardView,
  parseBoardView,
  toIssueFilter,
  toIssueOrder,
} from './viewState'
import type { BoardView } from './viewState'

/**
 * The URL round trip.
 *
 * This is the part of the board most worth pinning, and not because it is
 * intricate: it is the part whose failure is invisible. A filter that does not
 * reach the query string still filters -- the screen looks right, and only a
 * refresh, a shared link or the back button reveals that the view was never
 * really addressable. Nothing in the UI shows that regression, so a test has
 * to.
 *
 * The claims below are the three that matter: what goes out comes back, an
 * untouched view writes nothing, and a hand-edited URL cannot smuggle a value
 * into the view.
 */

const FULL: BoardView = {
  team: 'ENG',
  group: 'assignee',
  sort: 'due',
  status: 'started',
  assignee: '00000000-0000-4000-8000-00000000aa01',
  label: '00000000-0000-4000-8000-00000000bb02',
  priority: 1,
  project: '00000000-0000-4000-8000-00000000bb01',
  cycle: 'none',
}

/** A view, written to a query string and read back. */
function roundTrip(view: BoardView): BoardView {
  return parseBoardView(applyBoardView(new URLSearchParams(), view))
}

describe('board view state', () => {
  it('round-trips every field through the query string', () => {
    expect(roundTrip(FULL)).toEqual(FULL)
  })

  it('round-trips the defaults, which it writes nothing for', () => {
    const params = applyBoardView(new URLSearchParams(), DEFAULT_VIEW)

    // An untouched board is a clean URL. Two routes to the same view must
    // also produce the same link, which is why the defaults are omitted
    // rather than spelled out.
    expect(params.toString()).toBe('')
    expect(parseBoardView(params)).toEqual(DEFAULT_VIEW)
  })

  it('clears a filter from the query string rather than emptying it', () => {
    const withFilter = applyBoardView(new URLSearchParams(), FULL)
    const cleared = applyBoardView(withFilter, { ...FULL, assignee: null })

    expect(cleared.has('assignee')).toBe(false)
    expect(parseBoardView(cleared).assignee).toBeNull()
  })

  it('keeps parameters it does not own', () => {
    const base = new URLSearchParams('ref=slack&team=DES')
    const next = applyBoardView(base, { ...DEFAULT_VIEW, team: 'ENG' })

    // The board owns `team` and overwrites it; `ref` belongs to whoever put
    // it there and survives.
    expect(next.get('ref')).toBe('slack')
    expect(next.get('team')).toBe('ENG')
  })

  it('falls back to the defaults for a grouping or sort it does not know', () => {
    // A URL from an older build, or one somebody typed. Neither is an error
    // worth showing; both are a board.
    const view = parseBoardView(new URLSearchParams('group=colour&sort=vibes'))

    expect(view.group).toBe(DEFAULT_VIEW.group)
    expect(view.sort).toBe(DEFAULT_VIEW.sort)
  })

  it('refuses a status category this build cannot draw', () => {
    expect(parseBoardView(new URLSearchParams('status=started')).status).toBe('started')
    expect(parseBoardView(new URLSearchParams('status=STARTED')).status).toBe('started')
    expect(parseBoardView(new URLSearchParams('status=archived')).status).toBeNull()
  })

  it('drops a priority outside the range the server accepts', () => {
    expect(parseBoardView(new URLSearchParams('priority=0')).priority).toBe(0)
    expect(parseBoardView(new URLSearchParams('priority=4')).priority).toBe(4)

    // 5 is out of range, the empty string is `Number('') === 0` waiting to
    // happen, and `2x` is the one that looks numeric and is not.
    expect(parseBoardView(new URLSearchParams('priority=5')).priority).toBeNull()
    expect(parseBoardView(new URLSearchParams('priority=')).priority).toBeNull()
    expect(parseBoardView(new URLSearchParams('priority=2x')).priority).toBeNull()
  })

  it('treats an empty parameter as no filter at all', () => {
    const view = parseBoardView(new URLSearchParams('team=&assignee=&label=&project='))

    expect(view).toEqual(DEFAULT_VIEW)
  })
})

const TEAM_ID = '00000000-0000-4000-8000-00000000ee01'

/**
 * The view, as the server's own arguments.
 *
 * The half of the round trip that did not exist while the board filtered in
 * the browser, and the half with a trap in it: on `IssueFilterInput` an
 * explicit null is a *filter* on the three nullable columns -- unassigned,
 * unfiled, no cycle -- so "I am not filtering on this" can only be spelled by
 * leaving the key out. A filter object carrying `assigneeId: null` for a
 * board with no assignee filter is a valid request that returns a plausible
 * board of the wrong issues, which no rendering assertion would catch.
 */
describe('the view as a server filter', () => {
  it('sends the team and nothing else when nothing is filtered', () => {
    // `toEqual` on the whole object, not a property check: the claim is that
    // no other key exists, `undefined` ones included.
    expect(toIssueFilter(DEFAULT_VIEW, TEAM_ID)).toEqual({ teamId: TEAM_ID })
  })

  it('leaves an unused filter out rather than sending a null', () => {
    const filter = toIssueFilter({ ...DEFAULT_VIEW, priority: 2 }, TEAM_ID)

    expect(Object.keys(filter).toSorted()).toEqual(['priority', 'teamId'])
    expect('assigneeId' in filter).toBe(false)
    expect('projectId' in filter).toBe(false)
    expect('cycleId' in filter).toBe(false)
  })

  it('spells "the ones with none" as an explicit null, for each of the three', () => {
    const filter = toIssueFilter(
      { ...DEFAULT_VIEW, assignee: NONE, project: NONE, cycle: NONE },
      TEAM_ID,
    )

    expect(filter).toEqual({
      teamId: TEAM_ID,
      assigneeId: null,
      projectId: null,
      cycleId: null,
    })
  })

  it('carries every filter the controls offer', () => {
    const filter = toIssueFilter(
      {
        ...DEFAULT_VIEW,
        status: 'started',
        assignee: 'a1',
        label: 'l1',
        priority: 1,
        project: 'p1',
        cycle: 'c1',
      },
      TEAM_ID,
    )

    // The status filter is on the *category*, uppercased into the schema's
    // spelling: a team may call its started state anything.
    expect(filter).toEqual({
      teamId: TEAM_ID,
      stateCategory: 'STARTED',
      assigneeId: 'a1',
      labelId: 'l1',
      priority: 1,
      projectId: 'p1',
      cycleId: 'c1',
    })
  })
})

describe('the view as a server ordering', () => {
  it('gives each sort the direction it is worth reading in', () => {
    const order = (sort: BoardView['sort']) => toIssueOrder({ ...DEFAULT_VIEW, sort })

    // Ascending is NULLS LAST on the server, and the priority key is
    // `NULLIF(priority, 0)` -- so ascending is urgent first with the untriaged
    // work at the end, which is what a board asked for by priority means.
    expect(order('priority')).toEqual({ field: 'PRIORITY', direction: 'ASC' })

    // Soonest first, undated last, for the same reason.
    expect(order('due')).toEqual({ field: 'DUE_DATE', direction: 'ASC' })

    // Newest first is descending on both timestamps.
    expect(order('created')).toEqual({ field: 'CREATED_AT', direction: 'DESC' })
    expect(order('updated')).toEqual({ field: 'UPDATED_AT', direction: 'DESC' })
  })
})
