/**
 * The names this feature knows the server contract by.
 *
 * Aliases of generated types, every one. Nothing here declares a field, a
 * scalar or a nullability, so nothing here can disagree with the schema.
 */

import type { InitiativeStatus } from '../../../generated/schema'
import type {
  InitiativeCreateMutation,
  InitiativeDetailFieldsFragment,
  InitiativeFieldsFragment,
  InitiativeUpdateFieldsFragment,
} from '../../../generated/operations'

export type { Health, InitiativeStatus } from '../../../generated/schema'

/** One initiative, as a list row draws it. */
export type Initiative = InitiativeFieldsFragment

/** One initiative with its posted updates. */
export type InitiativeDetail = InitiativeDetailFieldsFragment

/** One posted update: a health, a sentence, an author and a time. */
export type InitiativeUpdateEntry = InitiativeUpdateFieldsFragment

/**
 * What the create and edit forms collect.
 *
 * Declared here rather than aliased to `InitiativeCreateInput`, and the
 * difference is deliberate: `workspaceSlug` is not something a form asks
 * about -- it comes from the URL, in ./mutations.ts -- and including it would
 * let a component supply one. The optional fields mirror the input's own
 * nullability, so a blank box travels as `null` and not as `''`.
 */
export interface InitiativeDraft {
  name: string
  description: string | null
  status: InitiativeStatus
  targetDate: string | null
  ownerId: string | null
}

/**
 * One entry of a payload's `errors`.
 *
 * An initiative the caller may not touch is refused with `code: "NOT_FOUND"`
 * -- the same answer a nonexistent id gets, deliberately. There is no
 * FORBIDDEN in `PUBLIC_ERROR_CODES`, so this screen never builds a "you do
 * not have permission" state: it cannot know.
 */
export type InitiativeValidationError =
  InitiativeCreateMutation['initiativeCreate']['errors'][number]
