import { statusCategoryFrom } from '../../../components'
import type { StatusCategory } from '../../../components'
import type { IssueRowFields, WorkflowState } from '../api'

/**
 * The order a grouped list runs in: the lifecycle, left to right.
 *
 * Not the states' own `position`, which is per team and therefore says
 * nothing about how two teams' states relate to each other. Category is the
 * one ordering the whole workspace agrees on, and it is the order the design
 * draws -- backlog, todo, in progress, done -- with cancelled last because it
 * is the exit, not a stage.
 */
const CATEGORY_ORDER: readonly StatusCategory[] = [
  'backlog',
  'unstarted',
  'started',
  'completed',
  'canceled',
]

export interface IssueGroup {
  /** Stable React key. */
  key: string
  /** The state's own name -- "In review" -- which is the group's label. */
  name: string
  /** Decides the header glyph's silhouette. */
  category: StatusCategory
  issues: readonly IssueRowFields[]
}

interface Bucket {
  key: string
  name: string
  category: StatusCategory
  order: number
  position: number
  issues: IssueRowFields[]
}

/**
 * One run of rows per workflow state, for the design's "Grouped" list mode.
 *
 * ## Grouped by state NAME, not by state id
 *
 * Workflow states belong to a team, so a workspace-wide list holds several
 * distinct "Todo" states -- one per team, each with its own id. Grouping by id
 * would draw three identically-labelled headers, which is the one thing a
 * grouped list must not do. Grouping by `category:name` merges them, and the
 * label is still a real state name the workspace uses rather than a category
 * word this file invented.
 *
 * Two teams that genuinely disagree -- an "Icebox" in one team and a "Backlog"
 * in another, both `BACKLOG` -- stay two groups, because they are two
 * different words for the reader.
 *
 * ## Null rather than a partial grouping
 *
 * A state that `stateById` cannot resolve has no group it could honestly go
 * in, and the common case for that is the workspace context still being in
 * flight -- when *every* state is unresolved. Returning null and letting the
 * caller render a flat list means the grouped view arrives complete instead of
 * flashing one anonymous bucket and then re-splitting.
 *
 * ponytail: sorts on every render. A list is one page of 25 rows, so this is
 * hundreds of comparisons; memoise on `issues` if a virtualised list ever
 * holds thousands.
 */
export function groupIssuesByState(
  issues: readonly IssueRowFields[],
  stateById: ReadonlyMap<string, WorkflowState>,
): readonly IssueGroup[] | null {
  const buckets = new Map<string, Bucket>()

  for (const issue of issues) {
    const state = stateById.get(issue.workflowStateId)

    if (state === undefined) {
      return null
    }

    const category = statusCategoryFrom(state.category)

    // A category this build has never heard of is a schema change, and a
    // group headed by a glyph we cannot draw is worse than no grouping.
    if (category === null) {
      return null
    }

    const key = `${category}:${state.name}`
    const existing = buckets.get(key)

    if (existing === undefined) {
      buckets.set(key, {
        key,
        name: state.name,
        category,
        order: CATEGORY_ORDER.indexOf(category),
        position: state.position,
        issues: [issue],
      })
      continue
    }

    existing.issues.push(issue)
    // Merged states can disagree on position; the earliest wins, so a
    // "Todo" that team A puts first does not sink because team B put its own
    // fourth.
    existing.position = Math.min(existing.position, state.position)
  }

  return [...buckets.values()].sort(
    (left, right) =>
      left.order - right.order ||
      left.position - right.position ||
      left.name.localeCompare(right.name),
  )
}
