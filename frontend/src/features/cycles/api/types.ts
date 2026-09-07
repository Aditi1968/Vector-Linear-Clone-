/**
 * The names this feature knows the server contract by.
 *
 * Aliases of generated types, every one. Nothing here declares a field, a
 * scalar or a nullability, so nothing here can disagree with the schema:
 * change ./operations.graphql, regenerate, and these follow. The generator
 * names a type after the construct that produced it and gives a *selection*
 * no name at all -- `CycleIssue` below is what the `CycleIssues` document
 * asks for, not the schema's `Issue` -- which is why the indexing.
 */

import type {
  CycleCreateInput as CycleCreateInputType,
  CycleUpdateInput as CycleUpdateInputType,
} from '../../../generated/schema'
import type {
  CycleCreateMutation,
  CycleFieldsFragment,
  CycleIssuesQuery,
  CycleTeamsQuery,
  CycleUnscheduledIssuesQuery,
} from '../../../generated/operations'

export type { CycleCreateInput, CycleUpdateInput } from '../../../generated/schema'

/** One cycle, as every screen in this feature receives it. */
export type CycleFields = CycleFieldsFragment

/** One team of the workspace, for the picker that chooses whose cycles to show. */
export type CycleTeam = CycleTeamsQuery['teams'][number]

/**
 * One issue, as this feature's document selects it.
 *
 * Narrower than the schema's `Issue` and narrower than it used to be: the
 * cycle it is in is what the query filtered on, so nothing renders it and
 * nothing selects it.
 */
export type CycleIssue = CycleIssuesQuery['issues']['nodes'][number]

/**
 * One candidate for the "add an issue" menu: a team issue in no cycle.
 *
 * A menu entry shows a name and nothing else, so this carries less again.
 */
export type CycleUnscheduledIssue =
  CycleUnscheduledIssuesQuery['issues']['nodes'][number]

/**
 * What the cycle form collects.
 *
 * Derived from the schema input with `Omit` rather than declared, so a field
 * added to `CycleCreateInput` appears here automatically and one removed
 * becomes a compile error in this file rather than a property silently
 * ignored on the wire. `workspaceSlug` comes from the URL and `teamId` from
 * the picker, so neither is something anybody types.
 */
export type CycleDraft = Omit<CycleCreateInputType, 'workspaceSlug' | 'teamId'>

/**
 * `CycleUpdateInput` requires `number`, `startsAt` and `endsAt` -- it is a
 * whole-row replace, not a patch, which is worth knowing before writing a
 * form that sends only what changed.
 */
export type CycleUpdateFields = Omit<CycleUpdateInputType, 'workspaceSlug' | 'id'>

/**
 * One entry of a payload's `errors`.
 *
 * Not GraphQL errors: they arrive inside `data` over a successful response
 * and describe user input. The one that matters most here is a cycle whose
 * dates overlap another of the team's -- `cycles_no_overlap` is a database
 * exclusion constraint, and the service turns the violation into a
 * structured error on `startsAt`/`endsAt` rather than into a 500.
 */
export type CycleValidationError = CycleCreateMutation['cycleCreate']['errors'][number]
