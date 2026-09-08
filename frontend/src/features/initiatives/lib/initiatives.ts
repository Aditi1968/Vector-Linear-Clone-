/**
 * The display and derivation rules the initiative screens share.
 *
 * Everything here is a pure function of what the server sent. Nothing invents
 * a value the API did not supply: an unparseable date comes back unchanged, a
 * project nobody can name is reported as such rather than replaced by a
 * plausible name, and an initiative whose parent is not in hand is not
 * silently promoted to the top level.
 */

import type { BadgeTone } from '../../../components'
import type { WorkspaceProject } from '../../issues/api'
import type { Health, Initiative, InitiativeStatus } from '../api'

/**
 * The four initiative statuses, named and toned.
 *
 * A `Record` keyed by the generated union, so a status added to the schema is
 * a compile error here rather than a blank badge in production. The labels
 * are this interface's convention; the API exposes only `PLANNED`, `ACTIVE`,
 * `COMPLETED`, `CANCELED`.
 */
const STATUS_PRESENTATION: Record<InitiativeStatus, { label: string; tone: BadgeTone }> = {
  PLANNED: { label: 'Planned', tone: 'neutral' },
  ACTIVE: { label: 'Active', tone: 'info' },
  COMPLETED: { label: 'Completed', tone: 'success' },
  CANCELED: { label: 'Canceled', tone: 'neutral' },
}

/** Every status, in the order a picker should offer them. */
export const INITIATIVE_STATUSES: readonly InitiativeStatus[] = [
  'PLANNED',
  'ACTIVE',
  'COMPLETED',
  'CANCELED',
]

export function initiativeStatusLabel(status: InitiativeStatus): string {
  return STATUS_PRESENTATION[status].label
}

export function initiativeStatusTone(status: InitiativeStatus): BadgeTone {
  return STATUS_PRESENTATION[status].tone
}

/**
 * The three healths, named and toned.
 *
 * Health is nullable on both `Initiative` and `Project`, and null is not a
 * fourth value: migration 022 is explicit that an initiative nobody has
 * posted an update on has NO health, which is a different fact from one
 * reported as on track. Callers render the null case themselves rather than
 * being handed a made-up label for it -- see `HEALTH_UNREPORTED`.
 */
const HEALTH_PRESENTATION: Record<Health, { label: string; tone: BadgeTone }> = {
  ON_TRACK: { label: 'On track', tone: 'success' },
  AT_RISK: { label: 'At risk', tone: 'warning' },
  OFF_TRACK: { label: 'Off track', tone: 'danger' },
}

/** Every health, in the order a picker should offer them. */
export const HEALTHS: readonly Health[] = ['ON_TRACK', 'AT_RISK', 'OFF_TRACK']

/**
 * What to say when `health` is null.
 *
 * Not "On track". The optimistic default is the tempting one and it is a lie
 * about a thing nobody has looked at, which is exactly the state a reader
 * most needs to be able to see.
 */
export const HEALTH_UNREPORTED = 'No update yet'

export function healthLabel(health: Health): string {
  return HEALTH_PRESENTATION[health].label
}

export function healthTone(health: Health): BadgeTone {
  return HEALTH_PRESENTATION[health].tone
}

/**
 * One row of the tree the list draws.
 *
 * `depth` is indentation, not a promise about the whole hierarchy: it is
 * measured within the initiatives actually loaded.
 */
export interface InitiativeTreeRow {
  initiative: Initiative
  depth: number
  /**
   * This initiative names a parent and is NOT drawn under it.
   *
   * It is drawn at the top level because there is nowhere else to draw it,
   * and the screen says so rather than letting it pass for a root. Two
   * things cause it, and the row reads the same either way -- "the nesting
   * you can see is not the whole nesting":
   *
   *   - the parent is on a page that has not been fetched, which "Load more"
   *     fixes;
   *   - the parent chain loops, which the server refuses at
   *     `initiativeSetParent` and which therefore should not exist. If one
   *     ever does, this is what stops the row disappearing.
   */
  parentNotShown: boolean
}

/**
 * Lay the loaded initiatives out as a forest.
 *
 * The server returns a flat page ordered newest-first and exposes the
 * hierarchy only as `parentInitiativeId` on the child and
 * `childInitiativeIds` on the parent. There is no query that returns a
 * subtree, so the nesting on screen is whatever can be reconstructed from the
 * rows in hand -- which is why `parentOffPage` exists rather than a quiet
 * fallback.
 *
 * Children keep the server's relative order within each parent.
 *
 * ## Every loaded initiative appears exactly once
 *
 * That is the property the two guards below exist for, and it does not fall
 * out of the obvious implementation. `initiativeSetParent` is the server's
 * place to refuse a cycle -- but a client that assumed it had, and walked
 * roots only, would emit NOTHING for a cycle, because every member of one has
 * a parent that IS loaded and so none of them is a root. A row silently
 * missing from a list is worse than a row drawn in the wrong place, so the
 * sweep at the end picks up anything the walk did not reach and draws it at
 * the top level, marked. The `seen` set is what keeps that walk finite.
 */
export function buildInitiativeTree(
  initiatives: readonly Initiative[],
): InitiativeTreeRow[] {
  const loaded = new Set(initiatives.map((initiative) => initiative.id))

  const childrenOf = new Map<string, Initiative[]>()
  const roots: Initiative[] = []

  for (const initiative of initiatives) {
    const parentId = initiative.parentInitiativeId

    if (parentId === null || !loaded.has(parentId)) {
      roots.push(initiative)
      continue
    }

    const siblings = childrenOf.get(parentId)

    if (siblings === undefined) {
      childrenOf.set(parentId, [initiative])
    } else {
      siblings.push(initiative)
    }
  }

  const rows: InitiativeTreeRow[] = []
  const seen = new Set<string>()

  const visit = (initiative: Initiative, depth: number) => {
    if (seen.has(initiative.id)) {
      return
    }

    seen.add(initiative.id)

    rows.push({
      initiative,
      depth,
      parentNotShown: depth === 0 && initiative.parentInitiativeId !== null,
    })

    for (const child of childrenOf.get(initiative.id) ?? []) {
      visit(child, depth + 1)
    }
  }

  for (const root of roots) {
    visit(root, 0)
  }

  // Anything the walk could not reach from a root: a cycle, which the server
  // refuses and which would otherwise make its members vanish from the list.
  for (const initiative of initiatives) {
    visit(initiative, 0)
  }

  return rows
}

/**
 * The projects an initiative holds, resolved to ones the viewer can name.
 *
 * `Initiative.projectIds` is a list of raw UUIDs; the schema exposes no
 * `Initiative.projects`. A name comes only from the workspace context's
 * `projects(first: 50)`, so an initiative holding a project outside that
 * first page has an id nothing here can resolve.
 *
 * That id is NOT dropped. It is a real membership, and silently shortening
 * the list would misstate what the initiative is -- the caller gets a null
 * project and says so. This mirrors `resolveTeams` in
 * `features/projects/lib/projects.ts`, for the same reason.
 */
export interface InitiativeProjectMembership {
  projectId: string
  project: WorkspaceProject | null
}

export function resolveProjects(
  projectIds: readonly string[],
  projects: readonly WorkspaceProject[],
): InitiativeProjectMembership[] {
  const byId = new Map(projects.map((project) => [project.id, project]))

  return projectIds.map((projectId) => ({
    projectId,
    project: byId.get(projectId) ?? null,
  }))
}

/**
 * The projects that could still be added to an initiative.
 *
 * Only the ones the workspace context returned, which is at most its first
 * page of 50. A workspace with more projects than that cannot offer the rest
 * here, and the screen says so beside the picker rather than presenting a
 * short list as the whole set.
 */
export function addableProjects(
  projectIds: readonly string[],
  projects: readonly WorkspaceProject[],
): WorkspaceProject[] {
  const held = new Set(projectIds)

  return projects.filter((project) => !held.has(project.id))
}

/**
 * The initiatives one initiative could be nested under.
 *
 * Itself excluded, and its own descendants too: `A under B under A` is a
 * cycle, and although the server refuses one, offering it in a picker invites
 * a refusal the user cannot have predicted. Descendants are computed from the
 * loaded rows, so an initiative whose children are on a later page may still
 * be offered -- the server is the backstop, not this list.
 */
export function parentCandidates(
  initiativeId: string,
  initiatives: readonly Initiative[],
): Initiative[] {
  const childrenOf = new Map<string, string[]>()

  for (const initiative of initiatives) {
    const parentId = initiative.parentInitiativeId

    if (parentId === null) {
      continue
    }

    const siblings = childrenOf.get(parentId)

    if (siblings === undefined) {
      childrenOf.set(parentId, [initiative.id])
    } else {
      siblings.push(initiative.id)
    }
  }

  const excluded = new Set<string>([initiativeId])
  const queue = [initiativeId]

  while (queue.length > 0) {
    // `pop` cannot return undefined inside a `length > 0` loop, but the
    // compiler does not know that and a non-null assertion here would be the
    // one place in this file the types were overruled.
    const current = queue.pop() ?? ''

    for (const child of childrenOf.get(current) ?? []) {
      if (excluded.has(child)) {
        continue
      }

      excluded.add(child)
      queue.push(child)
    }
  }

  return initiatives.filter((initiative) => !excluded.has(initiative.id))
}
