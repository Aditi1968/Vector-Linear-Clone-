import { describe, expect, it } from 'vitest'

import { issueRow } from '../../../test/factories'
import type { WorkflowState } from '../api'
import { groupIssuesByState } from './grouping'

/**
 * The grouped list mode.
 *
 * Unit tests rather than a render, because the cases that matter are about
 * states the shared fixture does not have: two teams whose "Todo" must merge,
 * two backlogs whose different names must not, and the unresolved state that
 * makes the whole grouping dishonest.
 */

function state(
  id: string,
  name: string,
  category: string,
  position: number,
): WorkflowState {
  return { __typename: 'WorkflowState', id, name, category, position, color: null } as WorkflowState
}

const ENG_TODO = state('s1', 'Todo', 'UNSTARTED', 0)
const APP_TODO = state('s2', 'Todo', 'UNSTARTED', 3)
const ENG_DOING = state('s3', 'In progress', 'STARTED', 1)
const ENG_ICEBOX = state('s4', 'Icebox', 'BACKLOG', 4)

function lookup(...states: WorkflowState[]): ReadonlyMap<string, WorkflowState> {
  return new Map(states.map((entry) => [entry.id, entry]))
}

describe('groupIssuesByState', () => {
  it('runs the groups in lifecycle order, not in the order rows arrive', () => {
    const groups = groupIssuesByState(
      [
        issueRow(1, { workflowStateId: ENG_DOING.id }),
        issueRow(2, { workflowStateId: ENG_ICEBOX.id }),
        issueRow(3, { workflowStateId: ENG_TODO.id }),
      ],
      lookup(ENG_TODO, ENG_DOING, ENG_ICEBOX),
    )

    expect(groups?.map((group) => group.name)).toEqual([
      'Icebox',
      'Todo',
      'In progress',
    ])
    expect(groups?.map((group) => group.category)).toEqual([
      'backlog',
      'unstarted',
      'started',
    ])
  })

  it('merges two teams that call their state the same thing', () => {
    // The whole reason grouping is by name and not by id: three headers all
    // reading "Todo" is the one thing a grouped list must not draw.
    const groups = groupIssuesByState(
      [
        issueRow(1, { workflowStateId: ENG_TODO.id }),
        issueRow(2, { workflowStateId: APP_TODO.id }),
      ],
      lookup(ENG_TODO, APP_TODO),
    )

    expect(groups).toHaveLength(1)
    expect(groups?.[0]?.issues).toHaveLength(2)
    // The earliest position of the merged states, so a "Todo" one team puts
    // first does not sink because another put its own fourth.
    expect(groups?.[0]?.name).toBe('Todo')
  })

  it('keeps two teams that use different words apart', () => {
    const groups = groupIssuesByState(
      [
        issueRow(1, { workflowStateId: ENG_ICEBOX.id }),
        issueRow(2, { workflowStateId: ENG_TODO.id }),
      ],
      lookup(ENG_ICEBOX, ENG_TODO),
    )

    expect(groups?.map((group) => group.name)).toEqual(['Icebox', 'Todo'])
  })

  it('refuses to group at all when a state cannot be resolved', () => {
    // The common case is the workspace context still being in flight. A
    // partial grouping would flash one anonymous bucket and then re-split.
    expect(
      groupIssuesByState([issueRow(1, { workflowStateId: 'missing' })], lookup(ENG_TODO)),
    ).toBeNull()
  })

  it('refuses to group on a category this build has never heard of', () => {
    const invented = state('s9', 'Parked', 'HIBERNATING', 0)

    expect(
      groupIssuesByState([issueRow(1, { workflowStateId: invented.id })], lookup(invented)),
    ).toBeNull()
  })

  it('is an empty grouping for an empty list, not null', () => {
    expect(groupIssuesByState([], lookup(ENG_TODO))).toEqual([])
  })
})
