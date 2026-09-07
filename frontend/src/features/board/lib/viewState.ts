/**
 * The board's filter, sort and grouping state, which lives in the URL.
 *
 * ## Why the URL and not `useState`
 *
 * Because a view of a board is a thing people send each other. "The urgent
 * unassigned work on ENG, grouped by assignee" is an address, and state held
 * in a component is an address that cannot be written down: it does not
 * survive a refresh, the back button does not undo a filter, and a link to it
 * opens somebody else's default view instead. Putting it in the query string
 * costs this module and buys all of that -- and it is the reason the round
 * trip through here is the most heavily tested thing in the feature.
 *
 * ## Almost everything here is a server argument
 *
 * `toIssueFilter` and `toIssueOrder` at the foot of this file translate a
 * view into `IssueFilterInput` and `IssueOrderInput`, which is where the
 * board's filters and sorting are applied. Only `group` stays in the browser:
 * there is no grouping argument, and there should not be -- a grouped board
 * needs every matching issue anyway, whichever column each lands in.
 *
 * ## Every value is validated on the way in
 *
 * A query string is user input -- typed, edited, or left over from an older
 * build. An unrecognised `group` or `sort` falls back to its default rather
 * than being trusted into a lookup, and a priority outside 0..4 is dropped.
 * The one thing this module deliberately does NOT validate is an id: whether
 * a label, project, cycle or assignee id names something real is a question
 * about loaded data, not about the URL, and a filter matching nothing renders
 * as an empty board rather than as an error.
 */

import { statusCategoryFrom } from '../../../components'
import type { StatusCategory } from '../../../components'
import type {
  IssueFilterInput,
  IssueOrderInput,
  WorkflowStateCategory,
} from '../../issues/api'
import { PRIORITY_VALUES } from '../../issues/lib/priority'

/** What the columns are. */
export const GROUP_BY = ['status', 'assignee', 'priority', 'project'] as const
export type GroupBy = (typeof GROUP_BY)[number]

/**
 * What orders the cards inside a column.
 *
 * Each has one natural direction and no toggle: priority runs most urgent
 * first, `created` and `updated` newest first, and `due` soonest first. A
 * direction control would double the states this module carries in order to
 * offer "the least recently updated card first", which is not a question
 * anyone asks a board.
 */
export const SORT_BY = ['priority', 'created', 'updated', 'due'] as const
export type SortBy = (typeof SORT_BY)[number]

/**
 * The filter value meaning "the ones with none".
 *
 * Unassigned, no project, no cycle. A sentinel rather than an empty string,
 * because an empty parameter is how "no filter" is spelled -- and it cannot
 * collide with a real id, which is always a UUID.
 */
export const NONE = 'none'

export interface BoardView {
  /** The team whose board this is, by key -- the ENG in ENG-42, not its UUID. */
  team: string | null
  group: GroupBy
  sort: SortBy
  /** A workflow state *category*, not one state: teams name their states freely. */
  status: StatusCategory | null
  /** A member's user id, or `NONE` for unassigned. */
  assignee: string | null
  /** A label id. One label, not a set -- see the note on `PARAM`. */
  label: string | null
  priority: number | null
  /** A project id, or `NONE`. */
  project: string | null
  /** A cycle id, or `NONE`. */
  cycle: string | null
}

/**
 * The query-string names, spelled once.
 *
 * Short and readable, because these end up in a link someone pastes into a
 * message. Each filter holds a single value; a multi-select would need its own
 * encoding, and every filter this board offers answers a question people ask
 * one at a time ("what is Ada working on", "what is urgent").
 */
const PARAM = {
  team: 'team',
  group: 'group',
  sort: 'sort',
  status: 'status',
  assignee: 'assignee',
  label: 'label',
  priority: 'priority',
  project: 'project',
  cycle: 'cycle',
} as const

/**
 * The view a URL with no parameters means.
 *
 * `team: null` is "whichever team the workspace lists first", resolved by the
 * screen against the teams it loaded -- this module has no way to know one.
 */
export const DEFAULT_VIEW: BoardView = {
  team: null,
  group: 'status',
  sort: 'priority',
  status: null,
  assignee: null,
  label: null,
  priority: null,
  project: null,
  cycle: null,
}

/** A parameter's value, or null when it is absent or empty. */
function read(params: URLSearchParams, name: string): string | null {
  const value = params.get(name)

  return value === null || value.length === 0 ? null : value
}

/** One of a fixed set, or the default for anything else. */
function readOneOf<Value extends string>(
  params: URLSearchParams,
  name: string,
  allowed: readonly Value[],
  fallback: Value,
): Value {
  const value = read(params, name)

  return allowed.find((candidate) => candidate === value) ?? fallback
}

/**
 * Read a board view out of a query string.
 *
 * Total: every input produces a view, because there is no such thing as a
 * malformed board URL that the user should be shown an error about.
 */
export function parseBoardView(params: URLSearchParams): BoardView {
  const priority = read(params, PARAM.priority)
  const parsedPriority = priority === null ? Number.NaN : Number(priority)

  return {
    team: read(params, PARAM.team),
    group: readOneOf(params, PARAM.group, GROUP_BY, DEFAULT_VIEW.group),
    sort: readOneOf(params, PARAM.sort, SORT_BY, DEFAULT_VIEW.sort),

    // Lowercase in the URL (`?status=started`) and narrowed by the same
    // function the indicators use, so a category this build cannot draw is
    // also a category it will not filter by.
    status: statusCategoryFrom(read(params, PARAM.status) ?? ''),

    assignee: read(params, PARAM.assignee),
    label: read(params, PARAM.label),

    // `Number('')` is 0 and `Number('1abc')` is NaN, so the emptiness check in
    // `read` happens first and the membership check catches the rest. A
    // priority outside what the server accepts is dropped rather than clamped.
    priority: PRIORITY_VALUES.includes(parsedPriority) ? parsedPriority : null,

    project: read(params, PARAM.project),
    cycle: read(params, PARAM.cycle),
  }
}

/**
 * Write a board view into a query string, preserving anything else in it.
 *
 * Defaults are omitted rather than spelled out, so an untouched board has a
 * clean URL and two ways of arriving at the same view produce the same link.
 * Parameters this module does not own are carried through untouched: the
 * board is not the only thing that may ever want the query string.
 */
export function applyBoardView(base: URLSearchParams, view: BoardView): URLSearchParams {
  const next = new URLSearchParams(base)

  const set = (name: string, value: string | null) => {
    if (value === null) {
      next.delete(name)
    } else {
      next.set(name, value)
    }
  }

  set(PARAM.team, view.team)
  set(PARAM.group, view.group === DEFAULT_VIEW.group ? null : view.group)
  set(PARAM.sort, view.sort === DEFAULT_VIEW.sort ? null : view.sort)
  set(PARAM.status, view.status)
  set(PARAM.assignee, view.assignee)
  set(PARAM.label, view.label)
  set(PARAM.priority, view.priority === null ? null : String(view.priority))
  set(PARAM.project, view.project)
  set(PARAM.cycle, view.cycle)

  return next
}

/** Whether any filter is narrowing the board. Sort and grouping are not filters. */
export function hasActiveFilter(view: BoardView): boolean {
  return (
    view.status !== null ||
    view.assignee !== null ||
    view.label !== null ||
    view.priority !== null ||
    view.project !== null ||
    view.cycle !== null
  )
}

/** The same view with every filter cleared, leaving the team, grouping and sort. */
export function clearFilters(view: BoardView): BoardView {
  return {
    ...view,
    status: null,
    assignee: null,
    label: null,
    priority: null,
    project: null,
    cycle: null,
  }
}

/** This build's lowercase category, as the schema spells it. */
const STATE_CATEGORY: Record<StatusCategory, WorkflowStateCategory> = {
  backlog: 'BACKLOG',
  unstarted: 'UNSTARTED',
  started: 'STARTED',
  completed: 'COMPLETED',
  canceled: 'CANCELED',
}

/**
 * A view, as the filter the server takes.
 *
 * ## The one rule this function exists to keep
 *
 * A filter that is not being applied must be an ABSENT KEY, never a null.
 * `IssueFilterInput` spends null on a *meaning* for the three nullable
 * columns: `assigneeId: null` is the unassigned issues, `projectId: null` the
 * unfiled ones, `cycleId: null` the backlog. So the object is built by adding
 * keys rather than by declaring them all and filling some in, and the board's
 * `NONE` sentinel -- which is exactly how a user asks for "the ones with
 * none" -- is the only thing that puts a null in.
 *
 * The failure this prevents is silent: `{ assigneeId: view.assignee }` with
 * no assignee filter is a valid request that returns a plausible board of the
 * wrong issues.
 *
 * `teamId` is always present. A board is one team's workflow states, so
 * another team's issues would arrive carrying a `workflowStateId` that
 * matches no column on screen and would simply vanish.
 */
export function toIssueFilter(view: BoardView, teamId: string): IssueFilterInput {
  const filter: IssueFilterInput = { teamId }

  if (view.status !== null) {
    filter.stateCategory = STATE_CATEGORY[view.status]
  }

  if (view.assignee !== null) {
    filter.assigneeId = view.assignee === NONE ? null : view.assignee
  }

  if (view.label !== null) {
    filter.labelId = view.label
  }

  if (view.priority !== null) {
    filter.priority = view.priority
  }

  if (view.project !== null) {
    filter.projectId = view.project === NONE ? null : view.project
  }

  if (view.cycle !== null) {
    filter.cycleId = view.cycle === NONE ? null : view.cycle
  }

  return filter
}

/**
 * A view, as the ordering the server takes.
 *
 * The directions are the ones each field is worth reading in, and they agree
 * with what the server does at the null end (`app/repositories/issues.py`:
 * ascending is NULLS LAST). Priority ascending is urgent first with the
 * untriaged work at the end, because the server sorts on
 * `NULLIF(priority, 0)` rather than on the column -- 0 means "no priority",
 * not "the lowest one". Due ascending is soonest first, with the undated
 * issues after every dated one: an issue with no due date is not due at the
 * end of time, it is simply not an answer to "what is due next".
 */
export function toIssueOrder(view: BoardView): IssueOrderInput {
  switch (view.sort) {
    case 'priority':
      return { field: 'PRIORITY', direction: 'ASC' }
    case 'created':
      return { field: 'CREATED_AT', direction: 'DESC' }
    case 'due':
      return { field: 'DUE_DATE', direction: 'ASC' }
    case 'updated':
    default:
      return { field: 'UPDATED_AT', direction: 'DESC' }
  }
}
