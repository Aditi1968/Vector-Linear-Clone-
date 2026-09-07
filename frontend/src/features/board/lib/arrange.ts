/**
 * Filtering, sorting and grouping, over the cards that have been loaded.
 *
 * Pure functions on plain data: no hooks, no Apollo, no React. That is what
 * makes the board's one genuinely fiddly piece testable without rendering
 * anything, and it is why the screen above reads as a sequence of three calls.
 *
 * ## "Among those loaded" is not a disclaimer, it is the contract
 *
 * The `issues` query takes a team and a cursor and nothing else. Everything
 * here therefore describes the pages already in the cache, and a column can be
 * empty because the matching issues have not been paged in yet -- not because
 * there are none. Every count this module produces is a count of loaded cards,
 * the screen says so in words, and nothing here is named `total`.
 */

import { statusCategoryFrom } from '../../../components'
import type { IssueRowFields, WorkflowState, WorkspaceMember } from '../../issues/api'
import { memberLabel } from '../../issues/api'
import { describePriority } from '../../issues/lib/priority'
import { NONE } from './viewState'
import type { BoardView } from './viewState'

/** What a column header shows when the grouping value is absent. */
const NO_ASSIGNEE = 'Unassigned'
const NO_PROJECT = 'No project'

export interface BoardColumn {
  /** Stable React key, and the drag-and-drop drop target's identity. */
  id: string
  /** The column heading: a state's name, a person's name, a priority, a project. */
  name: string
  /**
   * The workflow state this column *is*, when grouped by status.
   *
   * `null` in every other grouping, and that is what the screen branches on to
   * decide whether a card can be moved into this column at all: dropping a
   * card on an assignee column would have to mean reassigning it, which is a
   * different mutation and a different feature's decision to make.
   */
  state: WorkflowState | null
  /** The cards in this column, already filtered and sorted. */
  issues: readonly IssueRowFields[]
}

/** The lookups grouping needs, which the screen already holds for its cards. */
export interface BoardContext {
  /** The selected team's states, in the order the server gave them. */
  states: readonly WorkflowState[]
  /** `Issue.assigneeId` -> the person, for naming an assignee column. */
  memberById: ReadonlyMap<string, WorkspaceMember>
}

/**
 * Whether one card survives the filters.
 *
 * `states` resolves an issue's `workflowStateId` for the status filter, which
 * is a filter on the state's *category* rather than on the state: a team may
 * call its started state anything, and "show me what is in progress" is a
 * question about meaning. A card whose state is not among the team's -- which
 * would mean the issue moved teams underneath us -- fails a status filter
 * rather than passing one it was never checked against.
 */
export function matchesView(
  issue: IssueRowFields,
  view: BoardView,
  states: readonly WorkflowState[],
): boolean {
  if (view.status !== null) {
    const state = states.find((candidate) => candidate.id === issue.workflowStateId)

    if (state === undefined || statusCategoryFrom(state.category) !== view.status) {
      return false
    }
  }

  if (view.assignee !== null) {
    const matches =
      view.assignee === NONE
        ? issue.assigneeId === null
        : issue.assigneeId === view.assignee

    if (!matches) {
      return false
    }
  }

  if (view.label !== null && !issue.labels.some((label) => label.id === view.label)) {
    return false
  }

  if (view.priority !== null && issue.priority !== view.priority) {
    return false
  }

  if (view.project !== null) {
    const matches =
      view.project === NONE
        ? issue.project === null
        : issue.project?.id === view.project

    if (!matches) {
      return false
    }
  }

  if (view.cycle !== null) {
    const matches =
      view.cycle === NONE ? issue.cycle === null : issue.cycle?.id === view.cycle

    if (!matches) {
      return false
    }
  }

  return true
}

/**
 * Priority as a sortable rank.
 *
 * The wire values do not sort: 0 is "no priority" and 1..4 run from most to
 * least urgent (see `features/issues/lib/priority.ts`, which owns that
 * convention). Sorting on the integer would put the unprioritised work at the
 * top of every column. 0 becomes 5, which is the whole trick.
 */
function priorityRank(priority: number): number {
  return priority === 0 ? 5 : priority
}

/** Newest first, and a value that will not parse sorts last rather than first. */
function byTimestampDesc(left: string, right: string): number {
  return right.localeCompare(left)
}

/**
 * Order the cards in a column.
 *
 * Returns a new array: the input comes from Apollo, which freezes its results,
 * and `Array.prototype.sort` mutates in place.
 *
 * Every comparator falls back to `updatedAt` newest-first, so cards that tie
 * -- three unprioritised issues, two with no due date -- still come out in a
 * stable, meaningful order rather than in cursor order.
 */
export function sortIssues(
  issues: readonly IssueRowFields[],
  sort: BoardView['sort'],
): IssueRowFields[] {
  const sorted = [...issues]

  sorted.sort((left, right) => {
    switch (sort) {
      case 'priority': {
        const difference = priorityRank(left.priority) - priorityRank(right.priority)

        return difference === 0
          ? byTimestampDesc(left.updatedAt, right.updatedAt)
          : difference
      }

      case 'created':
        return byTimestampDesc(left.createdAt, right.createdAt)

      case 'due': {
        // Soonest first, and a card with no due date sorts after every card
        // that has one -- an undated issue is not "due at the end of time",
        // it is simply not in the answer to "what is due next".
        if (left.dueDate === null || right.dueDate === null) {
          return left.dueDate === right.dueDate
            ? byTimestampDesc(left.updatedAt, right.updatedAt)
            : left.dueDate === null
              ? 1
              : -1
        }

        return left.dueDate.localeCompare(right.dueDate)
      }

      case 'updated':
      default:
        return byTimestampDesc(left.updatedAt, right.updatedAt)
    }
  })

  return sorted
}

/** Cards into columns, keyed by whatever the grouping is. */
function bucket(
  issues: readonly IssueRowFields[],
  keyOf: (issue: IssueRowFields) => string,
): Map<string, IssueRowFields[]> {
  const buckets = new Map<string, IssueRowFields[]>()

  for (const issue of issues) {
    const key = keyOf(issue)
    const existing = buckets.get(key)

    if (existing === undefined) {
      buckets.set(key, [issue])
    } else {
      existing.push(issue)
    }
  }

  return buckets
}

/**
 * The board's columns, filtered, grouped and sorted.
 *
 * ## Status columns come from the team, every other grouping from the cards
 *
 * Grouped by status, the columns ARE the team's `workflowStates` in `position`
 * order -- all of them, including the empty ones, because an empty column is a
 * real part of a board and a status that disappears when nothing is in it is
 * not a board at all. Nothing here invents a Todo/Doing/Done: if a team calls
 * its states "Icebox" and "Shipped", those are the columns.
 *
 * Every other grouping derives its columns from the cards that survived the
 * filters, because the alternatives are worse: a column per workspace member
 * is forty empty columns in a real workspace, and a column per project is
 * every project the workspace has ever had.
 */
export function buildColumns(
  issues: readonly IssueRowFields[],
  view: BoardView,
  { states, memberById }: BoardContext,
): BoardColumn[] {
  const visible = issues.filter((issue) => matchesView(issue, view, states))
  const order = (grouped: readonly IssueRowFields[]) => sortIssues(grouped, view.sort)

  if (view.group === 'status') {
    const byState = bucket(visible, (issue) => issue.workflowStateId)

    // Copied before sorting: this array is Apollo's, and `sort` mutates.
    // `position` is the team's own ordering of its board and is what the
    // columns are laid out by -- never the order the array happened to arrive
    // in, and never the category.
    return [...states]
      .sort((left, right) => left.position - right.position)
      .map((state) => ({
        id: state.id,
        name: state.name,
        state,
        issues: order(byState.get(state.id) ?? []),
      }))
  }

  if (view.group === 'assignee') {
    const byAssignee = bucket(visible, (issue) => issue.assigneeId ?? NONE)

    return [...byAssignee.entries()]
      .map(([assigneeId, grouped]) => {
        const member = memberById.get(assigneeId)

        return {
          id: assigneeId,
          name:
            assigneeId === NONE
              ? NO_ASSIGNEE
              : // A member the workspace lookup does not know -- someone who
                // left, or a lookup that has not answered yet. The id is not a
                // name, so the column says what it can.
                (member === undefined ? 'Unknown member' : memberLabel(member)),
          state: null,
          issues: order(grouped),
        }
      })
      .sort(unassignedLast)
  }

  if (view.group === 'priority') {
    const byPriority = bucket(visible, (issue) => String(issue.priority))

    // Every priority, not only the ones present: unlike people and projects,
    // there are exactly five and they are a scale. A gap in a scale is
    // information -- "nothing urgent" is worth seeing.
    return [0, 1, 2, 3, 4]
      .sort((left, right) => priorityRank(left) - priorityRank(right))
      .map((priority) => {
        const { name } = describePriority(priority)

        return {
          id: String(priority),
          name: name ?? `Priority ${String(priority)}`,
          state: null,
          issues: order(byPriority.get(String(priority)) ?? []),
        }
      })
  }

  const byProject = bucket(visible, (issue) => issue.project?.id ?? NONE)

  return [...byProject.entries()]
    .map(([projectId, grouped]) => ({
      id: projectId,
      name: projectId === NONE ? NO_PROJECT : (grouped[0]?.project?.name ?? NO_PROJECT),
      state: null,
      issues: order(grouped),
    }))
    .sort(unassignedLast)
}

/**
 * Alphabetical, with the "has none" column at the end.
 *
 * Unassigned work and unfiled work belong after the named columns in both
 * groupings -- it is where the eye expects the leftovers, and it keeps the
 * column order stable as cards are assigned and filed.
 */
function unassignedLast(left: BoardColumn, right: BoardColumn): number {
  if (left.id === NONE || right.id === NONE) {
    return left.id === right.id ? 0 : left.id === NONE ? 1 : -1
  }

  return left.name.localeCompare(right.name)
}

/** One entry of a filter picker. `id` is `NONE` for the "has none" option. */
export interface FilterOption {
  id: string
  name: string
}

export interface BoardFilterOptions {
  assignees: readonly FilterOption[]
  labels: readonly FilterOption[]
  projects: readonly FilterOption[]
  cycles: readonly FilterOption[]
}

/** Collect distinct `{id, name}` pairs, in name order, with `NONE` last. */
function options(entries: readonly FilterOption[], hasNone: boolean, noneName: string) {
  const byId = new Map(entries.map((entry) => [entry.id, entry]))
  const sorted = [...byId.values()].sort((left, right) =>
    left.name.localeCompare(right.name),
  )

  return hasNone ? [...sorted, { id: NONE, name: noneName }] : sorted
}

/**
 * What the filter pickers offer.
 *
 * Derived from the loaded cards rather than from the workspace, and that is a
 * deliberate rule applied to all four: every option a picker offers can match
 * something currently on the board. `labels(workspaceSlug:)` and
 * `projects(workspaceSlug:)` exist and would give the complete sets -- at the
 * cost of two more requests and of pickers full of options that select
 * nothing, since the filtering happens over loaded pages either way. There is
 * no workspace-wide cycle query at all, so a cycle picker could not be
 * complete even in principle.
 *
 * Computed from ALL loaded cards, never from the filtered ones: pickers that
 * emptied each other as filters were applied would make a second filter
 * impossible to reach.
 */
export function filterOptions(
  issues: readonly IssueRowFields[],
  memberById: ReadonlyMap<string, WorkspaceMember>,
): BoardFilterOptions {
  const assignees: FilterOption[] = []
  const labels: FilterOption[] = []
  const projects: FilterOption[] = []
  const cycles: FilterOption[] = []
  let hasUnassigned = false
  let hasNoProject = false
  let hasNoCycle = false

  for (const issue of issues) {
    if (issue.assigneeId === null) {
      hasUnassigned = true
    } else {
      const member = memberById.get(issue.assigneeId)

      if (member !== undefined) {
        assignees.push({ id: issue.assigneeId, name: memberLabel(member) })
      }
    }

    for (const label of issue.labels) {
      labels.push({ id: label.id, name: label.name })
    }

    if (issue.project === null) {
      hasNoProject = true
    } else {
      projects.push({ id: issue.project.id, name: issue.project.name })
    }

    if (issue.cycle === null) {
      hasNoCycle = true
    } else {
      cycles.push({
        id: issue.cycle.id,
        // `Cycle.name` is nullable -- most cycles are known by their number.
        name: issue.cycle.name ?? `Cycle ${String(issue.cycle.number)}`,
      })
    }
  }

  return {
    assignees: options(assignees, hasUnassigned, NO_ASSIGNEE),
    labels: options(labels, false, ''),
    projects: options(projects, hasNoProject, NO_PROJECT),
    cycles: options(cycles, hasNoCycle, 'No cycle'),
  }
}
