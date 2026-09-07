/**
 * The names this feature knows the server contract by.
 *
 * Aliases of generated types, every one. Nothing here declares a field, a
 * scalar or a nullability, so nothing here can disagree with the schema.
 */

import type { IssueTemplateFieldsInput } from '../../../generated/schema'
import type {
  IssueCreateFromTemplateMutation,
  IssueTemplateCreateMutation,
  IssueTemplateFieldsFragment,
} from '../../../generated/operations'

export type { IssueTemplateFieldsInput } from '../../../generated/schema'

/** One template, whole. */
export type IssueTemplate = IssueTemplateFieldsFragment

/**
 * What a template form collects: every field of the stored row bar its id.
 *
 * This is `IssueTemplateFieldsInput` itself rather than a subset, and that is
 * the point. `IssueTemplateUpdateInput.template` is a WHOLE-ROW REPLACE, so
 * every field absent from a submission takes its schema default -- `null` for
 * the scalars, `[]` for `labelIds`. A draft type narrower than the input
 * would make it possible to write a form that silently cleared the fields it
 * did not render; making them the same type means the compiler asks for all
 * of them.
 */
export type IssueTemplateDraft = IssueTemplateFieldsInput

/** The issue a template produced. */
export type CreatedIssue = NonNullable<
  IssueCreateFromTemplateMutation['issueCreateFromTemplate']['issue']
>

/**
 * One entry of a payload's `errors`.
 *
 * A template the caller may not touch is refused with `code: "NOT_FOUND"` --
 * the same answer a nonexistent id gets, deliberately.
 */
export type TemplateValidationError =
  IssueTemplateCreateMutation['issueTemplateCreate']['errors'][number]
