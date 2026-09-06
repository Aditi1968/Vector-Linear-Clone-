import { describe, expect, it } from 'vitest'

import {
  cyclePhase,
  cycleTitle,
  formatCycleRange,
  fromDateTimeLocal,
  groupByPhase,
  toDateTimeLocal,
} from './cycles'
import type { CycleFields } from '../api'

/**
 * The cycle derivations, tested where they are either right or silently
 * wrong: at the interval's two boundaries.
 *
 * The backend labels nothing -- there is no `current` field and no phase enum
 * -- so this comparison is the only thing standing between `startsAt`/`endsAt`
 * and three sections on a screen. And it has to agree with the *database*,
 * which stores a cycle as `tstzrange(starts_at, ends_at, '[)')`: start
 * inclusive, end exclusive. An off-by-one at either end shows up as two
 * "current" cycles for one instant, or as none.
 */

const START = '2026-03-02T09:00:00.000Z'
const END = '2026-03-16T09:00:00.000Z'

function cycle(overrides: Partial<CycleFields> = {}): CycleFields {
  return {
    __typename: 'Cycle',
    id: '00000000-0000-4000-8000-000000000001',
    number: 12,
    name: null,
    startsAt: START,
    endsAt: END,
    ...overrides,
  }
}

describe('cyclePhase', () => {
  it('is upcoming one millisecond before it starts', () => {
    expect(cyclePhase(cycle(), Date.parse(START) - 1)).toBe('upcoming')
  })

  it('is current at the exact instant it starts', () => {
    // The `[` of `[)`: the start is inclusive, so a cycle beginning right now
    // is the current one and not a future one.
    expect(cyclePhase(cycle(), Date.parse(START))).toBe('current')
  })

  it('is current one millisecond before it ends', () => {
    expect(cyclePhase(cycle(), Date.parse(END) - 1)).toBe('current')
  })

  it('is past at the exact instant it ends', () => {
    // The `)` of `[)`: the end is EXCLUSIVE. This is the assertion that keeps
    // two back-to-back cycles from both being "current" for the instant they
    // share -- which is exactly what the database's exclusion constraint
    // permits and what a `<=` here would produce.
    expect(cyclePhase(cycle(), Date.parse(END))).toBe('past')
  })

  it('puts two abutting cycles in different phases at the shared instant', () => {
    const shared = Date.parse(END)
    const first = cycle({ startsAt: START, endsAt: END })
    const second = cycle({ startsAt: END, endsAt: '2026-03-30T09:00:00.000Z' })

    expect(cyclePhase(first, shared)).toBe('past')
    expect(cyclePhase(second, shared)).toBe('current')
  })

  it('files an unparseable cycle as past rather than as upcoming', () => {
    // Every comparison against NaN is false, so the naive branch order would
    // call this "upcoming" and float a broken row to the top of the screen.
    expect(cyclePhase(cycle({ startsAt: 'not a date' }), Date.now())).toBe('past')
  })
})

describe('groupByPhase', () => {
  const past = cycle({ id: 'a', number: 1, startsAt: '2026-01-05T00:00:00Z', endsAt: '2026-01-19T00:00:00Z' })
  const older = cycle({ id: 'b', number: 2, startsAt: '2025-12-01T00:00:00Z', endsAt: '2025-12-15T00:00:00Z' })
  const now = cycle({ id: 'c', number: 3, startsAt: '2026-03-02T00:00:00Z', endsAt: '2026-03-16T00:00:00Z' })
  const soon = cycle({ id: 'd', number: 4, startsAt: '2026-03-16T00:00:00Z', endsAt: '2026-03-30T00:00:00Z' })
  const later = cycle({ id: 'e', number: 5, startsAt: '2026-03-30T00:00:00Z', endsAt: '2026-04-13T00:00:00Z' })

  const instant = Date.parse('2026-03-09T00:00:00Z')

  it('splits every cycle into exactly one phase', () => {
    const grouped = groupByPhase([past, older, now, soon, later], instant)

    expect(grouped.current.map((entry) => entry.id)).toEqual(['c'])
    expect(grouped.upcoming.map((entry) => entry.id)).toEqual(['d', 'e'])
    expect(grouped.past.map((entry) => entry.id)).toEqual(['a', 'b'])
  })

  it('orders upcoming soonest-first and past most-recent-first', () => {
    // The server returns cycles in `number` order, which is neither of these.
    // Feeding them in reverse proves the sort is doing the work.
    const grouped = groupByPhase([later, soon, older, past], instant)

    expect(grouped.upcoming.map((entry) => entry.id)).toEqual(['d', 'e'])
    expect(grouped.past.map((entry) => entry.id)).toEqual(['a', 'b'])
  })

  it('returns three empty lists for a team with no cycles', () => {
    // The empty state a new team is in, and the one the screen has to render
    // three separate sentences for rather than a blank page.
    expect(groupByPhase([], instant)).toEqual({ current: [], upcoming: [], past: [] })
  })
})

describe('cycleTitle', () => {
  it('falls back to the number, which every cycle has', () => {
    expect(cycleTitle({ number: 12, name: null })).toBe('Cycle 12')
  })

  it('prefers the name when the team gave one', () => {
    expect(cycleTitle({ number: 12, name: 'Hardening' })).toBe('Hardening')
  })
})

describe('formatCycleRange', () => {
  it('returns an unparseable value unchanged rather than inventing a date', () => {
    expect(formatCycleRange({ startsAt: 'nope', endsAt: 'also nope' })).toBe('nope – also nope')
  })
})

describe('datetime-local conversion', () => {
  it('round-trips an instant through the native control format', () => {
    // The control has no timezone: it shows and returns local wall time. The
    // usual bug is `toISOString().slice(0, 16)`, which hands it UTC wall time
    // and shifts every cycle by the viewer's offset. A round trip is what
    // catches that, whatever timezone the test runs in.
    const instant = '2026-03-02T09:30:00.000Z'

    expect(fromDateTimeLocal(toDateTimeLocal(instant))).toBe(instant)
  })

  it('gives the control an empty string for a value it cannot show', () => {
    expect(toDateTimeLocal('')).toBe('')
    expect(fromDateTimeLocal('')).toBeNull()
  })
})
