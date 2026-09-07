import { useCallback } from 'react'
import { useMutation } from '@apollo/client/react'

import { useWorkspaceSlug } from '../../../app/routes'
import {
  CycleCreateDocument,
  CycleDeleteDocument,
  CycleUpdateDocument,
  IssueSetCycleDocument,
} from './documents'
import { describeError } from './errors'
import type { CycleDraft, CycleFields, CycleValidationError } from './types'

const UNEXPECTED_RESPONSE = 'That did not save. Please try again.'

/**
 * What a write can do, as three cases that cannot be confused.
 *
 * `rejected` is the payload's `errors`: expected input being refused,
 * arriving inside `data` over a 200, with a `field` naming what to fix. The
 * one that matters most here is a date range overlapping another of the
 * team's cycles. `failed` is a rejected promise -- an outage, a bug -- which
 * no field owns and which is shown once, at the top.
 */
export type CycleOutcome<T> =
  | { status: 'ok'; value: T }
  | { status: 'rejected'; errors: readonly CycleValidationError[] }
  | { status: 'failed'; message: string }

function readPayload<T>(
  payload: { errors: readonly CycleValidationError[] } | undefined,
  value: T | null | undefined,
): CycleOutcome<T> {
  if (payload === undefined) {
    return { status: 'failed', message: UNEXPECTED_RESPONSE }
  }

  // Checked first: the backend's contract is that exactly one of the two is
  // populated, and the errors are the more specific answer.
  if (payload.errors.length > 0) {
    return { status: 'rejected', errors: payload.errors }
  }

  if (value === null || value === undefined) {
    return { status: 'failed', message: UNEXPECTED_RESPONSE }
  }

  return { status: 'ok', value }
}

export interface UseCycleActionsResult {
  createCycle: (teamId: string, draft: CycleDraft) => Promise<CycleOutcome<CycleFields>>
  updateCycle: (id: string, draft: CycleDraft) => Promise<CycleOutcome<CycleFields>>
  deleteCycle: (id: string) => Promise<CycleOutcome<string>>
  /** Put an issue in a cycle, or take it out with `cycleId: null`. */
  setIssueCycle: (issueId: string, cycleId: string | null) => Promise<CycleOutcome<string>>
  isSaving: boolean
}

/**
 * Every write the cycle screens make.
 *
 * ## Which of these needs cache help
 *
 * `cycleUpdate` returns the cycle, so the normalised entity is corrected by
 * the response and both the list and the detail follow. Nothing to do.
 *
 * The other three change the *membership* of a list, which normalising an
 * entity never fixes, so each refetches by operation name -- which reruns
 * whichever query is mounted with the variables it was mounted with.
 *
 * Create and delete change `cycles(teamId:)`, a plain list field that neither
 * response contains. `issueSetCycle` changes two server-filtered connections:
 * the cycle's issues and the team's unscheduled ones. It selects the issue,
 * so the row itself is corrected -- but an issue taken out of the cycle would
 * stay in the panel until a reload, because correcting a row does not move it
 * out of a cached connection.
 */
export function useCycleActions(): UseCycleActionsResult {
  const workspaceSlug = useWorkspaceSlug()

  const [create, createState] = useMutation(CycleCreateDocument, {
    refetchQueries: ['CycleList'],
  })
  const [update, updateState] = useMutation(CycleUpdateDocument)
  const [remove, removeState] = useMutation(CycleDeleteDocument, {
    refetchQueries: ['CycleList'],
  })
  const [setCycle, setCycleState] = useMutation(IssueSetCycleDocument, {
    refetchQueries: ['CycleIssues', 'CycleUnscheduledIssues'],
  })

  const createCycle = useCallback(
    async (teamId: string, draft: CycleDraft) => {
      try {
        const result = await create({
          variables: { input: { ...draft, workspaceSlug, teamId } },
        })

        return readPayload(result.data?.cycleCreate, result.data?.cycleCreate.cycle)
      } catch (reason) {
        // The default `errorPolicy` of `none` makes `mutate` reject on a
        // top-level GraphQL error as well as on a transport failure, so this
        // is the only place either can arrive. Returned rather than
        // re-thrown: a failed save is an outcome the form renders.
        return { status: 'failed' as const, message: describeError(reason) }
      }
    },
    [create, workspaceSlug],
  )

  const updateCycle = useCallback(
    async (id: string, draft: CycleDraft) => {
      try {
        // Every field is sent, not just the changed ones: `CycleUpdateInput`
        // requires `number`, `startsAt` and `endsAt`, so this mutation is a
        // whole-row replace and a partial send would not type-check.
        const result = await update({ variables: { input: { ...draft, workspaceSlug, id } } })

        return readPayload(result.data?.cycleUpdate, result.data?.cycleUpdate.cycle)
      } catch (reason) {
        return { status: 'failed' as const, message: describeError(reason) }
      }
    },
    [update, workspaceSlug],
  )

  const deleteCycle = useCallback(
    async (id: string) => {
      try {
        // The one mutation in this schema taking loose arguments rather than
        // an input object. Called as the schema declares it.
        const result = await remove({ variables: { workspaceSlug, id } })
        const payload = result.data?.cycleDelete

        return readPayload(payload, payload?.deletedCycleId)
      } catch (reason) {
        return { status: 'failed' as const, message: describeError(reason) }
      }
    },
    [remove, workspaceSlug],
  )

  const setIssueCycle = useCallback(
    async (issueId: string, cycleId: string | null) => {
      try {
        const result = await setCycle({ variables: { input: { workspaceSlug, issueId, cycleId } } })
        const payload = result.data?.issueSetCycle

        return readPayload(payload, payload?.issue?.id)
      } catch (reason) {
        return { status: 'failed' as const, message: describeError(reason) }
      }
    },
    [setCycle, workspaceSlug],
  )

  return {
    createCycle,
    updateCycle,
    deleteCycle,
    setIssueCycle,
    isSaving:
      createState.loading || updateState.loading || removeState.loading || setCycleState.loading,
  }
}
