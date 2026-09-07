import { describe, expect, it } from 'vitest'

import { DEFAULT_VIEW, applyBoardView, parseBoardView } from './viewState'
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
