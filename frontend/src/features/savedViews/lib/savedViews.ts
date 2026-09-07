/**
 * The saved-view rules that are this frontend's, not the server's.
 *
 * Pure functions, no React and no Apollo, so the one genuinely subtle rule in
 * this feature -- which stored filters can be rewritten and which cannot --
 * is testable as arithmetic rather than through a rendered form.
 */

import type {
  IssueFilterInput,
  SavedViewFilter,
  SavedViewGrouping,
  SavedViewLayout,
  SavedViewVisibility,
} from '../api/types'

/**
 * Whether a stored filter can be sent back through `IssueFilterInput`.
 *
 * ## The asymmetry this exists for
 *
 * A saved view is read as a `SavedViewFilter` and written as an
 * `IssueFilterInput`, and the two do not describe the same set of filters.
 *
 * On the way out, `assignee`, `project` and `cycle` are `SavedViewIdFilter`
 * wrappers. The wrapper's *presence* is the filter and its `id` is what to
 * match, which gives three distinct states:
 *
 *     assignee: null          does not filter on assignee at all
 *     assignee: { id: "..." } assigned to that person
 *     assignee: { id: null }  UNASSIGNED -- the rows holding nothing
 *
 * On the way in, `IssueFilterInput` has a flat nullable `assigneeId`. A null
 * is indistinguishable from an absent key, so the input type can express the
 * first two states and *cannot express the third at all*.
 *
 * So a view stored with `assignee: {id: null}` -- "everything unassigned",
 * which is a perfectly reasonable and probably common saved view -- cannot be
 * round-tripped. Sending its filter back through an update would silently
 * turn "unassigned" into "any assignee", widening the view without saying so.
 *
 * This returns false for exactly that case, and the editor uses it to
 * disable filter editing and explain why rather than to quietly corrupt the
 * row. Name, visibility, layout, grouping and ordering stay editable, because
 * `SavedViewUpdateInput` takes every field optionally: a patch that omits
 * `filter` leaves the stored filter untouched.
 *
 * This is a schema gap and not a UI shortcoming. The fix is server-side --
 * an input that mirrors the wrapper -- and until it lands, refusing to
 * rewrite is the only honest option.
 */
export function isFilterRewritable(filter: SavedViewFilter): boolean {
  return [filter.assignee, filter.project, filter.cycle].every(
    (wrapper) => wrapper === null || wrapper === undefined || wrapper.id !== null,
  )
}

/**
 * A stored filter in the vocabulary the server accepts back.
 *
 * Returns null when the filter is not rewritable, so a caller cannot get a
 * lossy conversion by accident -- the only way to send a filter is to have
 * been handed one this function produced.
 *
 * A wrapper carrying a real id becomes that id; an absent wrapper becomes
 * null, which for `IssueFilterInput` means "do not filter on this".
 */
export function toFilterInput(filter: SavedViewFilter): IssueFilterInput | null {
  if (!isFilterRewritable(filter)) {
    return null
  }

  return {
    teamId: filter.teamId,
    workflowStateId: filter.workflowStateId,
    stateCategory: filter.stateCategory,
    labelId: filter.labelId,
    priority: filter.priority,
    assigneeId: filter.assignee?.id ?? null,
    projectId: filter.project?.id ?? null,
    cycleId: filter.cycle?.id ?? null,
  }
}

/** Whether a filter narrows anything at all. */
export function isFilterEmpty(filter: SavedViewFilter): boolean {
  return (
    filter.teamId === null &&
    filter.workflowStateId === null &&
    filter.stateCategory === null &&
    filter.labelId === null &&
    filter.priority === null &&
    (filter.assignee === null || filter.assignee === undefined) &&
    (filter.project === null || filter.project === undefined) &&
    (filter.cycle === null || filter.cycle === undefined)
  )
}

/**
 * How many columns a filter narrows on.
 *
 * For the list row's "3 filters" chip. Counting rather than naming, because
 * naming every column would need labels, projects, cycles and members
 * resolved for every row of the list -- four more documents for a summary
 * line. The editor names them; the row counts them.
 */
export function countFilters(filter: SavedViewFilter): number {
  const wrappers = [filter.assignee, filter.project, filter.cycle].filter(
    (wrapper) => wrapper !== null && wrapper !== undefined,
  ).length

  const scalars = [
    filter.teamId,
    filter.workflowStateId,
    filter.stateCategory,
    filter.labelId,
    filter.priority,
  ].filter((value) => value !== null && value !== undefined).length

  return wrappers + scalars
}

const VISIBILITY_LABELS: Record<SavedViewVisibility, string> = {
  PERSONAL: 'Personal',
  SHARED: 'Shared',
}

/** What a visibility is called. PERSONAL is its creator alone; SHARED is the workspace. */
export function visibilityLabel(value: SavedViewVisibility): string {
  return VISIBILITY_LABELS[value] ?? value
}

const LAYOUT_LABELS: Record<SavedViewLayout, string> = {
  LIST: 'List',
  BOARD: 'Board',
}

export function layoutLabel(value: SavedViewLayout): string {
  return LAYOUT_LABELS[value] ?? value
}

const GROUPING_LABELS: Record<SavedViewGrouping, string> = {
  WORKFLOW_STATE: 'Status',
  ASSIGNEE: 'Assignee',
  PRIORITY: 'Priority',
  PROJECT: 'Project',
  CYCLE: 'Cycle',
  TEAM: 'Team',
}

/**
 * What a grouping is called.
 *
 * There is deliberately no LABEL in this enum: an issue wears many labels, so
 * grouping by one would put the same issue in several groups and every count
 * drawn from them would overstate the list. That is the schema's reasoning,
 * repeated here only so nobody adds a sixth entry to this table.
 */
export function groupingLabel(value: SavedViewGrouping): string {
  return GROUPING_LABELS[value] ?? value
}

/** Every grouping the server will accept, for building a selector. */
export const GROUPING_VALUES = Object.keys(GROUPING_LABELS) as SavedViewGrouping[]
