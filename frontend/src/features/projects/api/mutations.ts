import { useCallback } from 'react'
import { useMutation } from '@apollo/client/react'

import { useWorkspaceSlug } from '../../../app/routes'
import {
  IssueSetProjectDocument,
  ProjectCreateDocument,
  ProjectDeleteDocument,
  ProjectMilestoneCreateDocument,
  ProjectMilestoneDeleteDocument,
  ProjectMilestoneUpdateDocument,
  ProjectTeamAddDocument,
  ProjectTeamRemoveDocument,
  ProjectUpdateDocument,
} from './documents'
import { describeError } from './errors'
import type {
  ProjectCreateInput,
  ProjectDetailFields,
  ProjectValidationError,
} from './types'

/**
 * Nothing came back that this code knows how to interpret. Distinct from a
 * validation rejection and from a transport failure, and rare enough that a
 * bespoke sentence per operation would be more alarming than useful.
 */
const UNEXPECTED_RESPONSE = 'That did not save. Please try again.'

/**
 * What a write can do, as three cases that cannot be confused.
 *
 * The two failure cases are different things and the backend returns them
 * through different channels:
 *
 *   - `rejected` is the payload's `errors`, a typed list arriving inside
 *     `data` over a successful response. The input was wrong, and every
 *     entry names the field it is about, so a form shows them beside the
 *     fields.
 *   - `failed` is a rejected promise: a network failure, an outage, a bug.
 *     No field owns it, so it is shown once, at the top.
 *
 * A discriminated union rather than `{ ok, errors, message }`, so a caller
 * cannot read `errors` out of a success or forget one of the two failure
 * paths -- the compiler makes it check.
 */
export type ProjectOutcome<T> =
  | { status: 'ok'; value: T }
  | { status: 'rejected'; errors: readonly ProjectValidationError[] }
  | { status: 'failed'; message: string }

/** Everything the project form collects: `ProjectCreateInput` minus the slug. */
export type ProjectDraft = Omit<ProjectCreateInput, 'workspaceSlug'>

/** What a milestone form collects. Position is the server's business. */
export interface MilestoneDraft {
  name: string
  targetDate: string | null
}

/**
 * Read a payload that carries an entity and an error list.
 *
 * `errors` is checked before the entity because the backend's contract is
 * that exactly one of the two is populated, and the errors are the more
 * specific answer.
 */
function readPayload<T>(
  payload: { errors: readonly ProjectValidationError[] } | undefined,
  value: T | null | undefined,
): ProjectOutcome<T> {
  if (payload === undefined) {
    return { status: 'failed', message: UNEXPECTED_RESPONSE }
  }

  if (payload.errors.length > 0) {
    return { status: 'rejected', errors: payload.errors }
  }

  if (value === null || value === undefined) {
    return { status: 'failed', message: UNEXPECTED_RESPONSE }
  }

  return { status: 'ok', value }
}

export interface UseCreateProjectResult {
  createProject: (draft: ProjectDraft) => Promise<ProjectOutcome<ProjectDetailFields>>
  isSubmitting: boolean
}

/**
 * Create a project.
 *
 * `refetchQueries` rather than a hand-written cache update, and the reason is
 * that a project's place in the list is the server's to decide: the list is
 * ordered `created_at DESC, id DESC` and paginated by an opaque keyset
 * cursor, so prepending locally would invent a position and leave the
 * cursor describing a frontier that no longer matches the rows on screen.
 * Named rather than passed as a document, which refetches the active
 * `ProjectList` with whatever variables it was mounted with -- the only
 * variables that are correct.
 */
export function useCreateProject(): UseCreateProjectResult {
  const workspaceSlug = useWorkspaceSlug()
  const [mutate, { loading }] = useMutation(ProjectCreateDocument, {
    refetchQueries: ['ProjectList'],
  })

  const createProject = useCallback(
    async (draft: ProjectDraft) => {
      try {
        const result = await mutate({ variables: { input: { ...draft, workspaceSlug } } })

        return readPayload(result.data?.projectCreate, result.data?.projectCreate.project)
      } catch (reason) {
        // The default `errorPolicy` of `none` makes `mutate` reject on a
        // top-level GraphQL error as well as on a transport failure, so this
        // is the only place either can arrive. Returned rather than
        // re-thrown: a failed create is an outcome the form renders, not an
        // exception that should take the page down.
        return { status: 'failed' as const, message: describeError(reason) }
      }
    },
    [mutate, workspaceSlug],
  )

  return { createProject, isSubmitting: loading }
}

export interface UseProjectActionsResult {
  updateProject: (draft: ProjectDraft) => Promise<ProjectOutcome<ProjectDetailFields>>
  deleteProject: () => Promise<ProjectOutcome<string>>
  addTeam: (teamId: string) => Promise<ProjectOutcome<ProjectDetailFields>>
  removeTeam: (teamId: string) => Promise<ProjectOutcome<ProjectDetailFields>>
  createMilestone: (draft: MilestoneDraft) => Promise<ProjectOutcome<string>>
  updateMilestone: (id: string, draft: MilestoneDraft) => Promise<ProjectOutcome<string>>
  deleteMilestone: (id: string) => Promise<ProjectOutcome<string>>
  /** Move an issue into this project, into one of its milestones, or out of both. */
  placeIssue: (
    issueId: string,
    projectId: string | null,
    milestoneId: string | null,
  ) => Promise<ProjectOutcome<string>>
  /** True while any of the above is in flight. */
  isSaving: boolean
}

/**
 * Every write the project detail screen makes.
 *
 * One hook rather than eight, because one screen makes all of them and eight
 * imports would say nothing extra. `isSaving` is the disjunction: the panels
 * disable together, which is also the honest behaviour, since two concurrent
 * edits to one project would race in an order neither the user nor this code
 * chose.
 *
 * ## Which writes need a refetch, and why only those
 *
 * `projectUpdate`, `projectTeamAdd` and `projectTeamRemove` all return the
 * whole `Project`, so the normalised entity -- `teamIds` included -- is
 * corrected by the response and every screen reading it follows. Nothing to
 * do.
 *
 * `projectMilestoneUpdate` returns the milestone, which is itself a
 * normalised entity that `Project.milestones` already points at. Also
 * nothing to do.
 *
 * Milestone *create* and *delete* are the two that need help, and for the
 * same reason: they change the membership of `Project.milestones`, a plain
 * list field, and neither response contains that list. A refetch of
 * `ProjectDetail` is one line and cannot go stale; `cache.modify` would be
 * three times the code to save one round trip on a list that is a handful of
 * rows long.
 *
 * `issueSetProject` selects `projectId` and `milestoneId` on the issue, so
 * the normalised issue is corrected and the screen's client-side filter
 * follows without a refetch.
 */
export function useProjectActions(projectId: string): UseProjectActionsResult {
  const workspaceSlug = useWorkspaceSlug()

  const [update, updateState] = useMutation(ProjectUpdateDocument)
  const [remove, removeState] = useMutation(ProjectDeleteDocument, {
    // The list is usually not mounted at this point (the user is on the
    // detail screen), so this is belt and braces for the case where it is --
    // a split view, or a return to a list still in the router's history.
    refetchQueries: ['ProjectList'],
  })
  const [teamAdd, teamAddState] = useMutation(ProjectTeamAddDocument)
  const [teamRemove, teamRemoveState] = useMutation(ProjectTeamRemoveDocument)
  const [milestoneCreate, milestoneCreateState] = useMutation(
    ProjectMilestoneCreateDocument,
    { refetchQueries: ['ProjectDetail'] },
  )
  const [milestoneUpdate, milestoneUpdateState] = useMutation(ProjectMilestoneUpdateDocument)
  const [milestoneDelete, milestoneDeleteState] = useMutation(
    ProjectMilestoneDeleteDocument,
    { refetchQueries: ['ProjectDetail'] },
  )
  const [setProject, setProjectState] = useMutation(IssueSetProjectDocument)

  const updateProject = useCallback(
    async (draft: ProjectDraft) => {
      try {
        // The whole draft is sent on every save, nulls included, and that is
        // deliberate: the backend distinguishes an *omitted* field (leave
        // alone) from an explicit null (clear it), so a form that omitted
        // its empty fields could never clear a description or a target date.
        const result = await update({
          variables: { input: { ...draft, workspaceSlug, id: projectId } },
        })

        return readPayload(result.data?.projectUpdate, result.data?.projectUpdate.project)
      } catch (reason) {
        return { status: 'failed' as const, message: describeError(reason) }
      }
    },
    [projectId, update, workspaceSlug],
  )

  const deleteProject = useCallback(async () => {
    try {
      const result = await remove({
        variables: { input: { workspaceSlug, id: projectId } },
      })

      return readPayload(
        result.data?.projectDelete,
        result.data?.projectDelete.deletedProjectId,
      )
    } catch (reason) {
      return { status: 'failed' as const, message: describeError(reason) }
    }
  }, [projectId, remove, workspaceSlug])

  const addTeam = useCallback(
    async (teamId: string) => {
      try {
        const result = await teamAdd({
          variables: { input: { workspaceSlug, projectId, teamId } },
        })

        return readPayload(result.data?.projectTeamAdd, result.data?.projectTeamAdd.project)
      } catch (reason) {
        return { status: 'failed' as const, message: describeError(reason) }
      }
    },
    [projectId, teamAdd, workspaceSlug],
  )

  const removeTeam = useCallback(
    async (teamId: string) => {
      try {
        const result = await teamRemove({
          variables: { input: { workspaceSlug, projectId, teamId } },
        })

        return readPayload(
          result.data?.projectTeamRemove,
          result.data?.projectTeamRemove.project,
        )
      } catch (reason) {
        return { status: 'failed' as const, message: describeError(reason) }
      }
    },
    [projectId, teamRemove, workspaceSlug],
  )

  const createMilestone = useCallback(
    async (draft: MilestoneDraft) => {
      try {
        const result = await milestoneCreate({
          variables: { input: { ...draft, workspaceSlug, projectId } },
        })
        const payload = result.data?.projectMilestoneCreate

        return readPayload(payload, payload?.milestone?.id)
      } catch (reason) {
        return { status: 'failed' as const, message: describeError(reason) }
      }
    },
    [milestoneCreate, projectId, workspaceSlug],
  )

  const updateMilestone = useCallback(
    async (id: string, draft: MilestoneDraft) => {
      try {
        const result = await milestoneUpdate({
          variables: { input: { ...draft, workspaceSlug, id } },
        })
        const payload = result.data?.projectMilestoneUpdate

        return readPayload(payload, payload?.milestone?.id)
      } catch (reason) {
        return { status: 'failed' as const, message: describeError(reason) }
      }
    },
    [milestoneUpdate, workspaceSlug],
  )

  const deleteMilestone = useCallback(
    async (id: string) => {
      try {
        const result = await milestoneDelete({
          variables: { input: { workspaceSlug, id } },
        })
        const payload = result.data?.projectMilestoneDelete

        return readPayload(payload, payload?.deletedMilestoneId)
      } catch (reason) {
        return { status: 'failed' as const, message: describeError(reason) }
      }
    },
    [milestoneDelete, workspaceSlug],
  )

  const placeIssue = useCallback(
    async (issueId: string, targetProjectId: string | null, milestoneId: string | null) => {
      try {
        const result = await setProject({
          variables: {
            input: { workspaceSlug, issueId, projectId: targetProjectId, milestoneId },
          },
        })
        const payload = result.data?.issueSetProject

        return readPayload(payload, payload?.issue?.id)
      } catch (reason) {
        return { status: 'failed' as const, message: describeError(reason) }
      }
    },
    [setProject, workspaceSlug],
  )

  return {
    updateProject,
    deleteProject,
    addTeam,
    removeTeam,
    createMilestone,
    updateMilestone,
    deleteMilestone,
    placeIssue,
    isSaving:
      updateState.loading ||
      removeState.loading ||
      teamAddState.loading ||
      teamRemoveState.loading ||
      milestoneCreateState.loading ||
      milestoneUpdateState.loading ||
      milestoneDeleteState.loading ||
      setProjectState.loading,
  }
}
