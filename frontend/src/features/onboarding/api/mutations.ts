import { useCallback } from 'react'
import { useMutation } from '@apollo/client/react'

import { describeError } from '../lib/errors'
import type { FieldError } from '../lib/errors'
import {
  OnboardingInvitationAcceptDocument,
  OnboardingInvitationCreateDocument,
  OnboardingTeamCreateDocument,
  OnboardingTeamsDocument,
  OnboardingWorkspaceCreateDocument,
  OnboardingWorkspacesDocument,
} from './documents'
import type {
  InvitationCreateInput,
  TeamCreateInput,
  WorkspaceCreateInput,
} from '../../../generated/schema'
import type {
  OnboardingInvitationAcceptMutation,
  OnboardingInvitationCreateMutation,
  OnboardingTeamCreateMutation,
  OnboardingWorkspaceCreateMutation,
} from '../../../generated/operations'

/**
 * Nothing came back that this code knows how to read. Distinct from both a
 * rejection and a transport failure, and rare enough that a bespoke sentence
 * per mutation would be more alarming than useful.
 */
const UNEXPECTED_RESPONSE = 'That did not work. Please try again.'

/**
 * What one of these mutations can do, as three cases that cannot be confused.
 *
 * A discriminated union rather than `{ ok, errors, message }` so a caller
 * cannot read `errors` out of a success or forget one of the two failure
 * paths -- the compiler makes it check. See ../lib/errors.ts for why the two
 * failures are separate cases rather than one.
 */
export type Outcome<TValue> =
  | { status: 'ok'; value: TValue }
  | { status: 'rejected'; errors: readonly FieldError[] }
  | { status: 'failed'; message: string }

interface PayloadLike {
  errors: readonly FieldError[]
}

/**
 * One `await mutate(...)`, normalised.
 *
 * Written once because all four mutations below share the same contract --
 * exactly one of `errors` and the value field is populated -- and four
 * hand-written copies of that reasoning is four chances to check them in the
 * wrong order. `errors` is checked before the value because the backend
 * populates one or the other and the errors are the more specific answer.
 */
async function outcomeOf<TValue>(
  send: () => Promise<{
    payload: PayloadLike | undefined
    value: TValue | null | undefined
  }>,
): Promise<Outcome<TValue>> {
  try {
    const { payload, value } = await send()

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
  } catch (reason) {
    /*
     * The default `errorPolicy` of `none` makes `mutate` reject on a
     * top-level GraphQL error as well as on a transport failure, so this is
     * the only place either arrives -- including this feature's two
     * refusals, `UNAUTHENTICATED` and the deliberately ambiguous "Workspace
     * not found". Returned rather than re-thrown: a failed step is something
     * the form renders, not an exception that should take the page down.
     */
    return { status: 'failed', message: describeError(reason) }
  }
}

type CreatedWorkspace = NonNullable<
  OnboardingWorkspaceCreateMutation['workspaceCreate']['workspace']
>
type CreatedTeam = NonNullable<OnboardingTeamCreateMutation['teamCreate']['team']>
type AcceptedWorkspace = NonNullable<
  OnboardingInvitationAcceptMutation['invitationAccept']['workspace']
>

/** A created invitation, together with the token shown exactly once. */
export interface CreatedInvitation {
  invitation: NonNullable<
    OnboardingInvitationCreateMutation['invitationCreate']['invitation']
  >
  token: string
}

/**
 * Create the workspace, and the caller's owner membership with it.
 *
 * `awaitRefetchQueries` matters here rather than being a nicety: the step
 * that follows this one is chosen by re-deriving progress from
 * `myWorkspaces`, so the caller must not be allowed to resume until that
 * query reflects the workspace that was just created. Without it the derived
 * state still says "no workspace", the guard redirects back to the step that
 * just succeeded, and the flow appears to lose the submission.
 */
export function useCreateWorkspace() {
  const [mutate, { loading }] = useMutation(OnboardingWorkspaceCreateDocument, {
    // Only when something was created. A rejected slug changes nothing on the
    // server, and `awaitRefetchQueries` would otherwise make someone wait for
    // a round trip that cannot alter the answer before being told their slug
    // was taken.
    refetchQueries: (result) =>
      result.data?.workspaceCreate.workspace == null
        ? []
        : [OnboardingWorkspacesDocument],
    awaitRefetchQueries: true,
  })

  const createWorkspace = useCallback(
    (input: WorkspaceCreateInput) =>
      outcomeOf<CreatedWorkspace>(async () => {
        const result = await mutate({ variables: { input } })
        const payload = result.data?.workspaceCreate

        return { payload, value: payload?.workspace }
      }),
    [mutate],
  )

  return { createWorkspace, isSubmitting: loading }
}

/** Create the first team. The server seeds its workflow states. */
export function useCreateTeam() {
  const [mutate, { loading }] = useMutation(OnboardingTeamCreateDocument, {
    // The document, not a `{ query, variables }` pair: this refetches every
    // *active* `OnboardingTeams` query with the variables it already has, so
    // the workspace slug does not have to be threaded through twice and
    // cannot be threaded through wrong. Skipped when nothing was created,
    // for the reason `useCreateWorkspace` gives.
    refetchQueries: (result) =>
      result.data?.teamCreate.team == null ? [] : [OnboardingTeamsDocument],
    awaitRefetchQueries: true,
  })

  const createTeam = useCallback(
    (input: TeamCreateInput) =>
      outcomeOf<CreatedTeam>(async () => {
        const result = await mutate({ variables: { input } })
        const payload = result.data?.teamCreate

        return { payload, value: payload?.team }
      }),
    [mutate],
  )

  return { createTeam, isSubmitting: loading }
}

/**
 * Invite one email address.
 *
 * No refetch: nothing in this feature reads the invitation list back, and
 * the one thing that matters about the response -- the token -- is not
 * readable from any query. The caller keeps it.
 */
export function useCreateInvitation() {
  const [mutate, { loading }] = useMutation(OnboardingInvitationCreateDocument)

  const createInvitation = useCallback(
    (input: InvitationCreateInput) =>
      outcomeOf<CreatedInvitation>(async () => {
        const result = await mutate({ variables: { input } })
        const payload = result.data?.invitationCreate

        // Both halves or neither. A payload with an invitation and a null
        // token would mean the one moment the token is obtainable had passed
        // with nothing shown, which is worse to report as a success than as a
        // failure.
        const value =
          payload?.invitation != null && payload.token != null
            ? { invitation: payload.invitation, token: payload.token }
            : null

        return { payload, value }
      }),
    [mutate],
  )

  return { createInvitation, isSubmitting: loading }
}

/**
 * Redeem a token.
 *
 * Refetches `myWorkspaces` because joining a workspace is exactly the event
 * that changes the answer to "is this person set up", and the guard that
 * asks reads that query.
 */
export function useAcceptInvitation() {
  const [mutate, { loading }] = useMutation(OnboardingInvitationAcceptDocument, {
    refetchQueries: [OnboardingWorkspacesDocument],
    awaitRefetchQueries: true,
  })

  const acceptInvitation = useCallback(
    (token: string) =>
      outcomeOf<AcceptedWorkspace>(async () => {
        const result = await mutate({ variables: { input: { token } } })
        const payload = result.data?.invitationAccept

        return { payload, value: payload?.workspace }
      }),
    [mutate],
  )

  return { acceptInvitation, isSubmitting: loading }
}
