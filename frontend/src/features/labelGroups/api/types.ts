/**
 * The names this feature knows the server contract by.
 *
 * Aliases of generated types, every one. Nothing here declares a field, a
 * scalar or a nullability, so nothing here can disagree with the schema.
 */

import type {
  GroupedLabelFieldsFragment,
  LabelGroupCreateMutation,
  LabelGroupFieldsFragment,
} from '../../../generated/operations'

/** One group: a name, and whether its labels exclude one another. */
export type LabelGroup = LabelGroupFieldsFragment

/** One label, with the group it belongs to -- or null, which is a real state. */
export type GroupedLabel = GroupedLabelFieldsFragment

/**
 * What the create and edit forms collect.
 *
 * `exclusive` is required rather than optional, and that mirrors the schema
 * rather than being stricter than it. `LabelGroupUpdateInput` declares both
 * `name: String!` and `exclusive: Boolean!` -- it is a whole-row replace, not
 * a patch, so a form that sent only the changed field would be sending an
 * incomplete input the server would refuse. `LabelGroupCreateInput` does allow
 * `exclusive` to be omitted; this form always sends it anyway, so the
 * application default in `app/services/labels.py` never has to decide
 * something the user was asked about.
 */
export interface LabelGroupDraft {
  name: string
  exclusive: boolean
}

/**
 * One entry of a payload's `errors`.
 *
 * The refusal this feature is most about arrives here: turning exclusivity ON
 * for a group whose labels already share an issue is rejected by PostgreSQL --
 * `labels_group_fk` cascades the flip down onto `issue_labels.exclusivity_key`
 * and `issue_labels_exclusive_group_key` refuses it. That failure is the
 * feature, per migration 021, and it is shown as itself.
 *
 * A group the caller may not touch is refused with `code: "NOT_FOUND"` -- the
 * same answer a nonexistent id gets, deliberately. There is no FORBIDDEN in
 * `PUBLIC_ERROR_CODES`, so this screen never builds a "you do not have
 * permission" state: it cannot know.
 */
export type LabelGroupValidationError =
  LabelGroupCreateMutation['labelGroupCreate']['errors'][number]
