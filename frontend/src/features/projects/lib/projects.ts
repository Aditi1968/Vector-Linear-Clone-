/**
 * The display and derivation rules the project screens share.
 *
 * Everything here is a pure function of what the server sent. Nothing invents
 * a value the API did not supply: an unparseable date comes back unchanged, a
 * lead nobody can see is reported as such rather than replaced by a plausible
 * name, and no count is estimated.
 */

import type { BadgeTone } from '../../../components'
import type { ProjectIssue, ProjectMember, ProjectState, ProjectTeam } from '../api'

/**
 * A calendar day, formatted in the viewer's locale.
 *
 * `timeZone: 'UTC'` is the whole point of this function existing rather than
 * a call to `toLocaleDateString` at each site. `targetDate` is the `Date`
 * scalar -- `YYYY-MM-DD`, a day every viewer in every timezone agrees on --
 * and `new Date('2026-03-14')` parses it as UTC *midnight*. Formatted in
 * local time west of Greenwich that renders as the 13th, so a project due on
 * the 14th would be shown as due the day before to everyone in the Americas.
 *
 * Locale is left as the user's; hardcoding `en-US` would put a German user's
 * dates in the wrong order for the sake of a stable screenshot.
 */
const DAY_FORMAT = new Intl.DateTimeFormat(undefined, {
  dateStyle: 'medium',
  timeZone: 'UTC',
})

export function formatDay(value: string): string {
  const parsed = new Date(value)

  return Number.isNaN(parsed.getTime()) ? value : DAY_FORMAT.format(parsed)
}

/**
 * The five project states, named and toned.
 *
 * A `Record` keyed by the generated union, so a state added to the schema is
 * a compile error here rather than a blank badge in production. The labels
 * are this interface's convention; the API exposes the states only as
 * `PLANNED`, `STARTED`, `PAUSED`, `COMPLETED`, `CANCELED`.
 */
const STATE_PRESENTATION: Record<ProjectState, { label: string; tone: BadgeTone }> = {
  PLANNED: { label: 'Planned', tone: 'neutral' },
  STARTED: { label: 'In progress', tone: 'info' },
  PAUSED: { label: 'Paused', tone: 'warning' },
  COMPLETED: { label: 'Completed', tone: 'success' },
  CANCELED: { label: 'Canceled', tone: 'neutral' },
}

/** Every state, in the order a picker should offer them. */
export const PROJECT_STATES: readonly ProjectState[] = [
  'PLANNED',
  'STARTED',
  'PAUSED',
  'COMPLETED',
  'CANCELED',
]

export function projectStateLabel(state: ProjectState): string {
  return STATE_PRESENTATION[state].label
}

export function projectStateTone(state: ProjectState): BadgeTone {
  return STATE_PRESENTATION[state].tone
}

/**
 * The teams a project is on, resolved to the ones the viewer can see.
 *
 * `Project.teamIds` is a list of raw UUIDs; a name for one comes only from
 * `teams(workspaceSlug:)`. An id in `teamIds` that the teams query did not
 * return is *not* dropped -- it is a real membership the viewer cannot
 * resolve, and silently shortening the list would misstate what the project
 * is. It comes back with a null team so the caller can say so.
 */
export interface ProjectTeamMembership {
  teamId: string
  team: ProjectTeam | null
}

export function resolveTeams(
  teamIds: readonly string[],
  teams: readonly ProjectTeam[],
): ProjectTeamMembership[] {
  const byId = new Map(teams.map((team) => [team.id, team]))

  return teamIds.map((teamId) => ({ teamId, team: byId.get(teamId) ?? null }))
}

/** The teams a project is *not* on. What the "add a team" picker offers. */
export function availableTeams(
  teamIds: readonly string[],
  teams: readonly ProjectTeam[],
): ProjectTeam[] {
  const joined = new Set(teamIds)

  return teams.filter((team) => !joined.has(team.id))
}

/**
 * A member's display name.
 *
 * `name` is nullable on `WorkspaceMember`, and an account that never set one
 * is identified by the address it signed up with rather than by a blank.
 */
export function memberName(member: ProjectMember): string {
  return member.name ?? member.email
}

/**
 * Who leads a project.
 *
 * Three genuinely different answers, and the third is the one usually got
 * wrong: no lead is set; a lead is set and known; a lead is set and *cannot
 * be resolved*, because the member list has not loaded, the viewer cannot see
 * that person, or the account was removed. Printing the raw UUID for the
 * third would be showing a database key to a human being.
 */
export function leadLabel(
  leadId: string | null,
  members: readonly ProjectMember[],
): { kind: 'none' } | { kind: 'named'; name: string } | { kind: 'unknown' } {
  if (leadId === null) {
    return { kind: 'none' }
  }

  const member = members.find((candidate) => candidate.userId === leadId)

  return member === undefined ? { kind: 'unknown' } : { kind: 'named', name: memberName(member) }
}

/**
 * How far along a set of issues is.
 *
 * `completedAt` is the only completion signal the issue selection carries,
 * and the schema is explicit that it is non-null "exactly while the issue
 * sits in a completed **or canceled** state". So this counts issues that are
 * *closed*, not issues that were finished, and every caller labels it that
 * way. Calling a canceled issue done would overstate progress, which is the
 * one direction a progress bar must never be wrong in.
 */
export function closedCount(issues: readonly ProjectIssue[]): number {
  return issues.filter((issue) => issue.completedAt !== null).length
}

/** The issues in one milestone, out of a project's issues. */
export function issuesInMilestone(
  issues: readonly ProjectIssue[],
  milestoneId: string,
): ProjectIssue[] {
  return issues.filter((issue) => issue.milestoneId === milestoneId)
}

/** The issues in a project that no milestone claims. */
export function issuesWithoutMilestone(issues: readonly ProjectIssue[]): ProjectIssue[] {
  return issues.filter((issue) => issue.milestoneId === null)
}
