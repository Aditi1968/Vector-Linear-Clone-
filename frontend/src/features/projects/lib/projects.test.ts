import { describe, expect, it } from 'vitest'

import {
  availableTeams,
  closedCount,
  formatDay,
  isTargetLate,
  issuesInMilestone,
  leadLabel,
  projectHealth,
  resolveTeams,
} from './projects'
import type { ProjectIssue, ProjectMember, ProjectTeam } from '../api'

/**
 * The project derivations, tested where they can be wrong in a way nobody
 * notices: the multi-team membership, the three answers to "who leads this",
 * and the two counts nothing in the API can be asked for.
 */

function team(id: string, key: string): ProjectTeam {
  return { __typename: 'Team', id, key, name: `${key} team` }
}

function member(userId: string, name: string | null): ProjectMember {
  return {
    __typename: 'WorkspaceMember',
    userId,
    name,
    email: `${userId}@example.test`,
    removedAt: null,
  }
}

function issue(overrides: Partial<ProjectIssue> = {}): ProjectIssue {
  return {
    __typename: 'Issue',
    id: 'i1',
    identifier: 'ENG-1',
    title: 'Something',
    completedAt: null,
    projectId: null,
    milestoneId: null,
    ...overrides,
  }
}

const ENG = team('t1', 'ENG')
const DES = team('t2', 'DES')

describe('resolveTeams', () => {
  it('keeps every membership, in the order the project states them', () => {
    // A project spanning teams is the product concept, not an edge case:
    // `Project.teamIds` is a list and there is no `project.team_id` anywhere.
    const resolved = resolveTeams([ENG.id, DES.id], [ENG, DES])

    expect(resolved.map((entry) => entry.team?.key)).toEqual(['ENG', 'DES'])
  })

  it('reports an unresolvable team rather than dropping it', () => {
    // The failure this exists to prevent: a viewer who cannot see one of the
    // teams would otherwise be shown a two-team project as a one-team
    // project -- a quiet misstatement, in the direction that hides work.
    const resolved = resolveTeams([ENG.id, 'invisible'], [ENG])

    expect(resolved).toHaveLength(2)
    expect(resolved[1]).toEqual({ teamId: 'invisible', team: null })
  })

  it('is empty for a project on no teams', () => {
    expect(resolveTeams([], [ENG, DES])).toEqual([])
  })
})

describe('availableTeams', () => {
  it('offers only the teams the project is not already on', () => {
    expect(availableTeams([ENG.id], [ENG, DES]).map((entry) => entry.key)).toEqual(['DES'])
  })

  it('offers nothing once every team is on the project', () => {
    // The screen renders no "Add team" control at all in this case, rather
    // than a disabled one over an empty menu.
    expect(availableTeams([ENG.id, DES.id], [ENG, DES])).toEqual([])
  })
})

describe('leadLabel', () => {
  const members = [member('u1', 'Ada'), member('u2', null)]

  it('says so when no lead is set', () => {
    expect(leadLabel(null, members)).toEqual({ kind: 'none' })
  })

  it('names a lead it can resolve', () => {
    expect(leadLabel('u1', members)).toEqual({ kind: 'named', name: 'Ada' })
  })

  it('falls back to the address for an account with no name', () => {
    expect(leadLabel('u2', members)).toEqual({ kind: 'named', name: 'u2@example.test' })
  })

  it('reports an unresolvable lead rather than printing the UUID', () => {
    // The member list has not loaded, the viewer cannot see that person, or
    // the account is gone. Showing a database key to a human being is not an
    // acceptable third answer.
    expect(leadLabel('u3', members)).toEqual({ kind: 'unknown' })
  })
})

describe('formatDay', () => {
  it('does not shift a calendar day west of Greenwich', () => {
    // `targetDate` is the `Date` scalar -- a day every viewer agrees on --
    // and `new Date('2026-03-14')` parses as UTC midnight. Formatted in local
    // time in the Americas that renders as the 13th. The assertion is on the
    // day number rather than the whole string, because the locale is the
    // user's and the format is not this test's business.
    expect(formatDay('2026-03-14')).toContain('14')
  })

  it('returns an unparseable value unchanged rather than inventing a date', () => {
    expect(formatDay('sometime')).toBe('sometime')
  })
})

describe('the derivations the server does not do', () => {
  // Which issues are the project's is `filter: { projectId }` now, so these
  // are the two questions left for the browser: which milestone an issue is
  // in, and how many of a set are closed. `ProjectMilestone` exposes no
  // counts of its own, so the second has nowhere else to be computed.
  const mine = issue({ id: 'a', projectId: 'p1', milestoneId: 'm1', completedAt: '2026-01-01T00:00:00Z' })
  const alsoMine = issue({ id: 'b', projectId: 'p1', milestoneId: null })

  it('keeps only the issues in one milestone', () => {
    expect(issuesInMilestone([mine, alsoMine], 'm1').map((entry) => entry.id)).toEqual(['a'])
  })

  it('counts a closed issue once and an open one not at all', () => {
    // `completedAt` is non-null for canceled issues too, which is why every
    // caller of this says "closed" rather than "done".
    expect(closedCount([mine, alsoMine])).toBe(1)
    expect(closedCount([])).toBe(0)
  })
})

describe('a late target date', () => {
  // Mid-afternoon on the 14th, in the timezone the suite happens to run in.
  // The instant matters: the comparison is day-against-day, and the bug this
  // pins is comparing a UTC midnight against a local `now`.
  const afternoonOfThe14th = new Date(2026, 2, 14, 15, 30).getTime()

  it('is not late on the day it is due, at any hour of that day', () => {
    const dawn = new Date(2026, 2, 14, 0, 1).getTime()

    expect(isTargetLate({ state: 'STARTED', targetDate: '2026-03-14' }, dawn)).toBe(false)
    expect(
      isTargetLate({ state: 'STARTED', targetDate: '2026-03-14' }, afternoonOfThe14th),
    ).toBe(false)
  })

  it('is late the day after', () => {
    expect(
      isTargetLate({ state: 'STARTED', targetDate: '2026-03-13' }, afternoonOfThe14th),
    ).toBe(true)
  })

  it('is never late once the project is closed', () => {
    // A project that finished after its target is a fact about the past. An
    // alarm nobody can act on is not a reading.
    expect(
      isTargetLate({ state: 'COMPLETED', targetDate: '2026-01-01' }, afternoonOfThe14th),
    ).toBe(false)
    expect(
      isTargetLate({ state: 'CANCELED', targetDate: '2026-01-01' }, afternoonOfThe14th),
    ).toBe(false)
  })

  it('says nothing about a project with no target, or an unparseable one', () => {
    expect(isTargetLate({ state: 'STARTED', targetDate: null }, afternoonOfThe14th)).toBe(false)
    expect(isTargetLate({ state: 'STARTED', targetDate: 'someday' }, afternoonOfThe14th)).toBe(
      false,
    )
  })
})

describe('projectHealth', () => {
  const now = new Date(2026, 2, 14, 15, 30).getTime()

  it('reads a running project on its date as on track', () => {
    expect(projectHealth({ state: 'STARTED', targetDate: '2026-03-14' }, now)).toBe('on-track')
  })

  it('reads a paused project as at risk', () => {
    expect(projectHealth({ state: 'PAUSED', targetDate: '2026-04-01' }, now)).toBe('at-risk')
  })

  it('reads a running project past its date as off track', () => {
    // The one thing that makes `off-track` mean something. There is no health
    // field in the schema, so without this the hue would never be reached.
    expect(projectHealth({ state: 'STARTED', targetDate: '2026-03-01' }, now)).toBe('off-track')
  })

  it('reads a project that has not begun, and one that was called off, as planned', () => {
    expect(projectHealth({ state: 'PLANNED', targetDate: '2026-03-01' }, now)).toBe('planned')
    expect(projectHealth({ state: 'CANCELED', targetDate: '2026-03-01' }, now)).toBe('planned')
  })

  it('does not call a finished project late', () => {
    expect(projectHealth({ state: 'COMPLETED', targetDate: '2026-01-01' }, now)).toBe('on-track')
  })
})
