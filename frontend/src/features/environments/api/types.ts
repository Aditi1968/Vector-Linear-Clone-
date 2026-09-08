/**
 * The names this feature knows the server contract by.
 *
 * Aliases of generated types, every one. Nothing here declares a field, a
 * scalar or a nullability, so nothing here can disagree with the schema.
 */

import type {
  EnvironmentCreateMutation,
  EnvironmentFieldsFragment,
} from '../../../generated/operations'

export type { EnvironmentKind } from '../../../generated/schema'

/** One deploy target. */
export type Environment = EnvironmentFieldsFragment

/**
 * What the create form collects.
 *
 * `workspaceSlug` is not here: it comes from the URL, in ./mutations.ts, and
 * including it would let a component supply one.
 */
export interface EnvironmentDraft {
  name: string
  kind: EnvironmentFieldsFragment['kind']
}

/**
 * One entry of a payload's `errors`.
 *
 * The one this screen meets most is the duplicate name --
 * `environments_workspace_name_key` in migration 024 refuses two environments
 * called "Production", because whichever one a deploy script picked would be
 * the one nobody was watching.
 */
export type EnvironmentValidationError =
  EnvironmentCreateMutation['environmentCreate']['errors'][number]
