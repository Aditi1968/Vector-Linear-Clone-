import { useCallback, useState } from 'react'
import { useMutation, useQuery } from '@apollo/client/react'

import { useWorkspaceSlug } from '../../app/routes'
import { describeError } from '../screens'
import {
  MemberInvitationCreateDocument,
  MemberInvitationRevokeDocument,
  MemberRemoveDocument,
  MemberRoleUpdateDocument,
  WorkspaceInvitationListDocument,
  WorkspaceMemberListDocument,
} from '../../generated/operations'
import type {
  MemberInvitationCreateMutation,
  MemberRemoveMutation,
  WorkspaceInvitationListQuery,
  WorkspaceMemberListQuery,
} from '../../generated/operations'
import type { WorkspaceRole } from '../../generated/schema'

/**
 * The members screen's data adapter.
 *
 * The boundary the screen is written against: it imports the hooks and types
 * below and never a document, an Apollo hook, or an Apollo error type. The
 * documents live in ./operations.graphql and are re-exported here only
 * because mocking a response in a test requires the exact document that
 * produced it.
 */

export {
  MemberInvitationCreateDocument,
  MemberInvitationRevokeDocument,
  MemberRemoveDocument,
  MemberRoleUpdateDocument,
  WorkspaceInvitationListDocument,
  WorkspaceMemberListDocument,
} from '../../generated/operations'

/** One person in the workspace, and the role they hold. */
export type WorkspaceMemberRow = WorkspaceMemberListQuery['workspaceMembers'][number]

/** One invitation that has not been accepted or expired. */
export type WorkspaceInvitationRow = WorkspaceInvitationListQuery['invitations'][number]

/** The token, returned once, together with the invitation it belongs to. */
export type CreatedInvitation = {
  invitation: NonNullable<MemberInvitationCreateMutation['invitationCreate']['invitation']>
  token: string
}

/**
 * One entry of a payload's `errors`. Not a GraphQL error -- see below.
 *
 * Indexed out of one of this feature's own payloads rather than named from
 * the schema, so it means "what actually arrives" and follows the documents
 * if their selection ever changes. Every payload here selects the same three
 * fields, so any of the five would do.
 */
export type MemberValidationError =
  MemberRemoveMutation['memberRemove']['errors'][number]

export type { WorkspaceRole }

/**
 * What a write to this screen can do, as three cases that cannot be confused.
 *
 * `rejected` is the payload's own `errors`: a typed list arriving inside
 * `data` over a successful response, describing input the server refused --
 * a malformed email, the last owner being removed, a caller whose role is not
 * enough. `failed` is a rejected promise: a network failure, an outage, a
 * top-level GraphQL error. A discriminated union rather than
 * `{ ok, errors, message }` so the caller cannot read `errors` out of a
 * success or forget one of the two failure paths.
 */
export type MemberOutcome<Value> =
  | { status: 'ok'; value: Value }
  | { status: 'rejected'; errors: readonly MemberValidationError[] }
  | { status: 'failed'; message: string }

const UNEXPECTED = 'The change could not be saved. Please try again.'

/** Stable identities for "nothing yet", so memoised children are not defeated. */
const NO_MEMBERS: readonly WorkspaceMemberRow[] = []
const NO_INVITATIONS: readonly WorkspaceInvitationRow[] = []

export interface UseMembersResult {
  members: readonly WorkspaceMemberRow[]
  isLoading: boolean
  errorMessage: string | null
  retry: () => void
}

/** Everyone in the workspace. Members only, so this works for any viewer here. */
export function useMembers(): UseMembersResult {
  const workspaceSlug = useWorkspaceSlug()

  const { data, error, loading, refetch } = useQuery(WorkspaceMemberListDocument, {
    variables: { workspaceSlug },
  })

  const retry = useCallback(() => {
    // Swallowed on purpose: `refetch` rejects *and* sets `error` on the hook
    // result, and `error` is what the screen renders.
    void refetch().catch(() => undefined)
  }, [refetch])

  return {
    members: data?.workspaceMembers ?? NO_MEMBERS,
    isLoading: loading,
    errorMessage: error === undefined ? null : describeError(error),
    retry,
  }
}

export interface UseInvitationsResult {
  invitations: readonly WorkspaceInvitationRow[]
  isLoading: boolean
  errorMessage: string | null
  retry: () => void
}

/**
 * Outstanding invitations. Admins and owners only.
 *
 * `enabled` is a hint and not a gate. Passing `false` skips the request,
 * which is the right thing to do for a viewer whose role cannot read this --
 * it avoids an error the screen would only discard. It is not what makes the
 * data safe: the server authorizes every scoped field against membership and
 * role regardless of what the client sends or believes, and a viewer whose
 * role changed underneath them gets the refusal in `errorMessage` rather than
 * an empty panel that reads as "no invitations".
 */
export function useInvitations(enabled: boolean): UseInvitationsResult {
  const workspaceSlug = useWorkspaceSlug()

  const { data, error, loading, refetch } = useQuery(WorkspaceInvitationListDocument, {
    variables: { workspaceSlug },
    skip: !enabled,
  })

  const retry = useCallback(() => {
    void refetch().catch(() => undefined)
  }, [refetch])

  return {
    invitations: data?.invitations ?? NO_INVITATIONS,
    // `skip` leaves `loading` false, which is right: nothing is on its way.
    isLoading: loading,
    errorMessage: error === undefined ? null : describeError(error),
    retry,
  }
}

export interface UseMemberActionsResult {
  invite: (email: string, role: WorkspaceRole) => Promise<MemberOutcome<CreatedInvitation>>
  revokeInvitation: (id: string) => Promise<MemberOutcome<string>>
  changeRole: (userId: string, role: WorkspaceRole) => Promise<MemberOutcome<string>>
  removeMember: (userId: string) => Promise<MemberOutcome<string>>
  isSubmitting: boolean
}

/**
 * The four writes this screen makes.
 *
 * Every one refetches the list it changed rather than patching the cache, and
 * that is the schema talking rather than laziness. `WorkspaceMember` and
 * `WorkspaceInvitation` results either cannot be normalised (`WorkspaceMember`
 * has `userId` and no `id`) or describe a *removal*, which no normalised
 * write can express -- so three of the four would need a hand-written cache
 * update anyway, and four hand-written updates are four places for the list
 * on screen to drift from the list on the server.
 *
 * `awaitRefetchQueries` is left at its default. The screen reports its own
 * submitting state from the mutation, and blocking it on a refetch would
 * leave the button spinning after the write it names has already landed.
 */
export function useMemberActions(): UseMemberActionsResult {
  const workspaceSlug = useWorkspaceSlug()
  const [isSubmitting, setIsSubmitting] = useState(false)

  const memberList = {
    query: WorkspaceMemberListDocument,
    variables: { workspaceSlug },
  }
  const invitationList = {
    query: WorkspaceInvitationListDocument,
    variables: { workspaceSlug },
  }

  const [createInvitation] = useMutation(MemberInvitationCreateDocument, {
    refetchQueries: [invitationList],
  })
  const [revoke] = useMutation(MemberInvitationRevokeDocument, {
    refetchQueries: [invitationList],
  })
  const [updateRole] = useMutation(MemberRoleUpdateDocument, {
    refetchQueries: [memberList],
  })
  const [remove] = useMutation(MemberRemoveDocument, {
    refetchQueries: [memberList],
  })

  /**
   * Run one write and read its two failure channels the same way every time.
   *
   * `errors` is checked before the value, because the backend's contract is
   * that exactly one of the two is populated and the errors are the more
   * specific answer.
   */
  const run = useCallback(
    async <Value,>(
      send: () => Promise<{
        errors: readonly MemberValidationError[]
        value: Value | null
      }>,
    ): Promise<MemberOutcome<Value>> => {
      setIsSubmitting(true)

      try {
        const { errors, value } = await send()

        if (errors.length > 0) {
          return { status: 'rejected', errors }
        }

        if (value === null) {
          return { status: 'failed', message: UNEXPECTED }
        }

        return { status: 'ok', value }
      } catch (reason: unknown) {
        // Returned as an outcome rather than re-thrown: a failed write is
        // something the UI renders, not an exception that takes the page down.
        return { status: 'failed', message: describeError(reason) }
      } finally {
        setIsSubmitting(false)
      }
    },
    [],
  )

  const invite = useCallback(
    (email: string, role: WorkspaceRole) =>
      run(async () => {
        const result = await createInvitation({
          variables: { input: { workspaceSlug, email, role } },
        })
        const payload = result.data?.invitationCreate

        return {
          errors: payload?.errors ?? [],
          // Both halves or neither: an invitation without its token is an
          // invitation nobody can redeem, and reporting it as a success
          // would mean the one moment the token was obtainable had passed.
          value:
            payload?.invitation != null && payload.token != null
              ? { invitation: payload.invitation, token: payload.token }
              : null,
        }
      }),
    [createInvitation, run, workspaceSlug],
  )

  const revokeInvitation = useCallback(
    (id: string) =>
      run(async () => {
        const result = await revoke({ variables: { input: { workspaceSlug, id } } })
        const payload = result.data?.invitationRevoke

        return {
          errors: payload?.errors ?? [],
          value: payload?.revokedInvitationId ?? null,
        }
      }),
    [revoke, run, workspaceSlug],
  )

  const changeRole = useCallback(
    (userId: string, role: WorkspaceRole) =>
      run(async () => {
        const result = await updateRole({
          variables: { input: { workspaceSlug, userId, role } },
        })
        const payload = result.data?.memberRoleUpdate

        return {
          errors: payload?.errors ?? [],
          value: payload?.member?.userId ?? null,
        }
      }),
    [run, updateRole, workspaceSlug],
  )

  const removeMember = useCallback(
    (userId: string) =>
      run(async () => {
        const result = await remove({ variables: { input: { workspaceSlug, userId } } })
        const payload = result.data?.memberRemove

        return {
          errors: payload?.errors ?? [],
          value: payload?.removedUserId ?? null,
        }
      }),
    [remove, run, workspaceSlug],
  )

  return { invite, revokeInvitation, changeRole, removeMember, isSubmitting }
}
