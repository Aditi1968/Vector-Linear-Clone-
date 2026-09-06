import { useQuery } from '@apollo/client/react'

import { useWorkspaceSlug } from '../../../app/routes'
import { TeamCyclesDocument } from './documents'
import type { TeamCycle } from './types'

const NO_CYCLES: readonly TeamCycle[] = []

/**
 * The cycles one team runs.
 *
 * Separate from `useWorkspaceContext` because `cycles(teamId:)` is required
 * to name a team, and the team is a property of whichever issue is open --
 * which is not known when the screen mounts. Skipped entirely until there
 * is one, so the list view never sends this.
 */
export function useTeamCycles(teamId: string | undefined): readonly TeamCycle[] {
  const workspaceSlug = useWorkspaceSlug()

  const { data } = useQuery(TeamCyclesDocument, {
    // `teamId` still has to type-check while the query is skipped, so an id
    // that will not be sent is passed as the empty string rather than
    // smuggled past the type with a cast.
    variables: { workspaceSlug, teamId: teamId ?? '' },
    skip: teamId === undefined,
  })

  return data?.cycles ?? NO_CYCLES
}
