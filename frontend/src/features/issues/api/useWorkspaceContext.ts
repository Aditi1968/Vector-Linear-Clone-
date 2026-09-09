import { useCallback, useMemo } from 'react'
import { useQuery } from '@apollo/client/react'

import { useWorkspaceSlug } from '../../../app/routes'
import { describeError } from '../lib/errors'
import { IssueWorkspaceContextDocument } from './documents'
import type {
  WorkflowState,
  WorkspaceMember,
  WorkspaceProject,
  WorkspaceTeam,
} from './types'

/** Stable identities for "nothing yet", so a first render does not churn memos. */
const NO_TEAMS: readonly WorkspaceTeam[] = []
const NO_MEMBERS: readonly WorkspaceMember[] = []
const NO_PROJECTS: readonly WorkspaceProject[] = []

export interface WorkspaceContext {
  /** Teams, in the order the server returned them. */
  teams: readonly WorkspaceTeam[]
  /**
   * Everyone the workspace has had, including people who have left.
   *
   * For resolving a name, never for offering a choice. An issue created or
   * assigned by somebody who has since been removed still has to say who they
   * were; `activeMembers` is what a picker reads.
   */
  members: readonly WorkspaceMember[]
  /** The people still here -- the only ones who can be given future work. */
  activeMembers: readonly WorkspaceMember[]
  /** Every project an issue can be placed in. */
  projects: readonly WorkspaceProject[]

  /** `Issue.workflowStateId` -> the state it names. */
  stateById: ReadonlyMap<string, WorkflowState>
  /** `Issue.assigneeId` -> the person it names. */
  memberById: ReadonlyMap<string, WorkspaceMember>
  /** `Issue.teamId` -> that team, whose `workflowStates` are its board. */
  teamById: ReadonlyMap<string, WorkspaceTeam>

  /** Whether the lookups are still empty because the answer has not arrived. */
  isLoading: boolean
  /**
   * Why the lookups are empty, when the reason is that the request failed.
   *
   * Read by the screens for which a team is a *precondition* rather than a
   * lookup -- the board and the triage queue, neither of which can be drawn
   * without one. Every other consumer ignores it deliberately; see the note
   * on the hook.
   */
  errorMessage: string | null
  /** Ask again. The recovery path out of `errorMessage`. */
  retry: () => void
}

/**
 * The lookups every issue row and every issue inspector reads.
 *
 * ## Why this exists at all
 *
 * `Issue.assigneeId` and `Issue.workflowStateId` are raw UUIDs. The schema
 * exposes no `Issue.assignee` and no `Issue.workflowState`, so a row that
 * wants to draw a status glyph or an avatar has to resolve the id itself --
 * through `teams(workspaceSlug:)` for states and `workspaceMembers(
 * workspaceSlug:)` for people.
 *
 * Doing that per row would be 25 requests per page, or (worse) 25 identical
 * ones that Apollo happens to deduplicate today. This is one document for the
 * whole screen, read from the cache by every consumer, with the maps built
 * once per response rather than once per row.
 *
 * ## Why the maps are memoised on `data` and not on the arrays
 *
 * Apollo returns a structurally stable `data` object while nothing changes,
 * so keying the memo on it rebuilds the maps exactly when a response
 * genuinely differs. Keying on `data?.teams` would be equivalent and would
 * need three dependencies to say the same thing.
 *
 * ## Empty is a state, not an error -- except where it is a claim
 *
 * A workspace with no teams, no projects or one member is ordinary. Most
 * consumers read these as *lookups*: the pickers render what there is, the
 * composer refuses to submit when there is no team to file into, and a failed
 * request degrades a row to its ids rather than replacing a whole list with an
 * error panel about a name resolution. Those consumers still ignore the
 * failure, and should.
 *
 * But two screens treat a team as a *precondition* and say so on screen. The
 * board and the triage queue both render "No teams in this workspace" when
 * this comes back with none -- and an empty array is what a failed request
 * leaves behind too, so a dropped connection made both of them assert
 * something about the workspace that nothing had measured. That is the one
 * thing an empty state may never do.
 *
 * So the failure is reported rather than swallowed. Whether it matters is the
 * caller's decision, which is the only place that knows whether it is about to
 * draw a lookup or make a claim.
 */
export function useWorkspaceContext(): WorkspaceContext {
  const workspaceSlug = useWorkspaceSlug()

  const { data, error, loading, refetch } = useQuery(IssueWorkspaceContextDocument, {
    variables: { workspaceSlug },
  })

  const retry = useCallback(() => {
    // Swallowed: `refetch` rejects *and* sets `error` on the hook result, and
    // `error` is what the screen renders.
    void refetch().catch(() => undefined)
  }, [refetch])

  return useMemo(() => {
    const teams = data?.teams ?? NO_TEAMS
    const members = data?.workspaceMembers ?? NO_MEMBERS
    const projects = data?.projects.nodes ?? NO_PROJECTS

    const stateById = new Map<string, WorkflowState>()
    const teamById = new Map<string, WorkspaceTeam>()

    for (const team of teams) {
      teamById.set(team.id, team)

      for (const state of team.workflowStates) {
        // Workflow state ids are unique across the workspace, not merely
        // within a team, so one flat map answers `Issue.workflowStateId`
        // without the caller having to know the issue's team first.
        stateById.set(state.id, state)
      }
    }

    const memberById = new Map(members.map((member) => [member.userId, member]))

    // Filtered here rather than on the server, and rather than at each picker.
    // `workspaceMembers` deliberately returns both halves in one list -- see
    // the note on `MembershipRepository.list_members` -- because a screen that
    // fetched only the active ones would render a former member's work as
    // authored by nobody, which looks exactly like a lookup that failed. So
    // the split happens once, by field, where every consumer can see it.
    const activeMembers = members.filter((member) => member.removedAt === null)

    return {
      teams,
      members,
      activeMembers,
      projects,
      stateById,
      memberById,
      teamById,
      isLoading: loading,
      errorMessage: error === undefined ? null : describeError(error),
      retry,
    }
  }, [data, error, loading, retry])
}

/**
 * What to call someone.
 *
 * `WorkspaceMember.name` is nullable -- an invited account that has never set
 * one -- and the email is the only other thing the schema gives, so it is
 * what a nameless member is shown as. Never the raw UUID: an id is not a
 * person's name, and showing one in an assignee column is how a list stops
 * being readable.
 */
export function memberLabel(member: WorkspaceMember): string {
  return member.name ?? member.email
}
