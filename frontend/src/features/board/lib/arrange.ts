/**
 * Grouping, over the cards the server sent.
 *
 * Pure functions on plain data: no hooks, no Apollo, no React. That is what
 * makes the board's one genuinely fiddly piece testable without rendering
 * anything.
 *
 * ## Why grouping is the only thing left here
 *
 * Filtering and ordering are `IssueFilterInput` and `IssueOrderInput` now
 * (../lib/viewState.ts translates the view into both), so the cards arrive
 * already narrowed and already in order and this module neither re-filters
 * nor re-sorts them. Grouping stays because there is no grouping argument --
 * correctly: a grouped board needs every matching issue anyway, whichever
 * column each lands in, so a server that grouped would return the same rows
 * in a different envelope.
 *
 * The cards are still one *page* of the matching issues. That is the screen's
 * sentence to write, not this module's, and nothing here is named `total`.
 */

import type { BoardLabel } from '../api'
import type {
  IssueRowFields,
  TeamCycle,
  WorkflowState,
  WorkspaceMember,
  WorkspaceProject,
} from '../../issues/api'
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
 * Priority as a sortable rank, for laying the priority columns out.
 *
 * The wire values do not sort: 0 is "no priority" and 1..4 run from most to
 * least urgent (see `features/issues/lib/priority.ts`, which owns that
 * convention). Ordering columns by the integer would put the unprioritised
 * column first. 0 becomes 5, which is the whole trick -- and it is the same
 * rule the server sorts *cards* by, where it is spelled `NULLIF(priority, 0)`.
 */
function priorityRank(priority: number): number {
  return priority === 0 ? 5 : priority
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
 * The board's columns.
 *
 * The cards arrive filtered and ordered from the server, so this only decides
 * which column each belongs in. Order *within* a column is the order the
 * cards came in, preserved by the bucketing rather than recomputed: the
 * server's ordering is total (it ends in the issue's id), so re-sorting here
 * could only disagree with it.
 *
 * ## Status columns come from the team, every other grouping from the cards
 *
 * Grouped by status, the columns ARE the team's `workflowStates` in `position`
 * order -- all of them, including the empty ones, because an empty column is a
 * real part of a board and a status that disappears when nothing is in it is
 * not a board at all. Nothing here invents a Todo/Doing/Done: if a team calls
 * its states "Icebox" and "Shipped", those are the columns.
 *
 * Every other grouping derives its columns from the cards, because the
 * alternatives are worse: a column per workspace member is forty empty
 * columns in a real workspace, and a column per project is every project the
 * workspace has ever had.
 */
export function buildColumns(
  issues: readonly IssueRowFields[],
  view: BoardView,
  { states, memberById }: BoardContext,
): BoardColumn[] {
  if (view.group === 'status') {
    const byState = bucket(issues, (issue) => issue.workflowStateId)

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
        issues: byState.get(state.id) ?? [],
      }))
  }

  if (view.group === 'assignee') {
    const byAssignee = bucket(issues, (issue) => issue.assigneeId ?? NONE)

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
          issues: grouped,
        }
      })
      .sort(unassignedLast)
  }

  if (view.group === 'priority') {
    const byPriority = bucket(issues, (issue) => String(issue.priority))

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
          issues: byPriority.get(String(priority)) ?? [],
        }
      })
  }

  const byProject = bucket(issues, (issue) => issue.project?.id ?? NONE)

  return [...byProject.entries()]
    .map(([projectId, grouped]) => ({
      id: projectId,
      name: projectId === NONE ? NO_PROJECT : (grouped[0]?.project?.name ?? NO_PROJECT),
      state: null,
      issues: grouped,
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

/** In name order, with the "has none" option -- when there is one -- last. */
function options(entries: readonly FilterOption[], noneName?: string) {
  const sorted = [...entries].sort((left, right) => left.name.localeCompare(right.name))

  return noneName === undefined ? sorted : [...sorted, { id: NONE, name: noneName }]
}

/** What the four pickers are built from. */
export interface BoardFilterSources {
  members: readonly WorkspaceMember[]
  projects: readonly WorkspaceProject[]
  labels: readonly BoardLabel[]
  /** The selected team's cycles: a cycle belongs to a team, so a board has a set. */
  cycles: readonly TeamCycle[]
}

/**
 * What the filter pickers offer.
 *
 * The workspace's own lists, not the loaded cards'. It was the other way
 * around while the filtering happened in the browser, on the argument that an
 * option matching nothing on the board was noise. Server-side filtering
 * inverts that argument: once a filter is applied the loaded cards are only
 * the ones that match it, so a picker built from them would offer the option
 * already selected and nothing else -- a control that cannot be changed
 * without first being cleared.
 *
 * The "has none" option is offered unconditionally by the three filters that
 * have one, because it is a question the server can always answer -- an
 * explicit null on a nullable column -- rather than a value that has to be
 * present among the cards before it can be asked for.
 */
export function filterOptions({
  members,
  projects,
  labels,
  cycles,
}: BoardFilterSources): BoardFilterOptions {
  return {
    assignees: options(
      members.map((member) => ({ id: member.userId, name: memberLabel(member) })),
      NO_ASSIGNEE,
    ),
    labels: options(labels.map((label) => ({ id: label.id, name: label.name }))),
    projects: options(
      projects.map((project) => ({ id: project.id, name: project.name })),
      NO_PROJECT,
    ),
    cycles: options(
      cycles.map((cycle) => ({
        id: cycle.id,
        // `Cycle.name` is nullable -- most cycles are known by their number.
        name: cycle.name ?? `Cycle ${String(cycle.number)}`,
      })),
      'No cycle',
    ),
  }
}
