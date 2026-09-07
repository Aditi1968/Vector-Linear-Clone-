/**
 * The names this feature knows the server contract by.
 *
 * Aliases of generated types, every one. Nothing here declares a field, a
 * scalar or a nullability, so nothing here can disagree with the schema.
 */

import type {
  IssueFilterInput as IssueFilterInputType,
  SavedViewCreateInput as SavedViewCreateInputType,
} from '../../../generated/schema'
import type {
  SavedViewCreateMutation,
  SavedViewFieldsFragment,
  SavedViewResultsQuery,
} from '../../../generated/operations'

export type {
  IssueFilterInput,
  IssueOrderField,
  OrderDirection,
  SavedViewGrouping,
  SavedViewLayout,
  SavedViewVisibility,
  WorkflowStateCategory,
} from '../../../generated/schema'

/** One saved view, as every screen in this feature receives it. */
export type SavedViewFields = SavedViewFieldsFragment

/**
 * The stored filter, as it reads back.
 *
 * NOT the same type as the filter that is written. `SavedViewFilter` wraps
 * `assignee`, `project` and `cycle` in a `SavedViewIdFilter`, where the
 * wrapper's presence is the filter and its `id` is the value -- so
 * `{id: null}` means "unassigned" and a null wrapper means "does not filter
 * on assignee". `IssueFilterInput`, which is what create and update take, has
 * flat nullable `assigneeId`/`projectId`/`cycleId` and cannot tell those two
 * apart. ../lib/savedViews.ts is where that asymmetry is handled instead of
 * being papered over.
 */
export type SavedViewFilter = SavedViewFields['filter']

/** One issue a saved view selected. A full `Issue`, unlike a triage row. */
export type SavedViewIssue = NonNullable<
  SavedViewResultsQuery['savedView']
>['issues']['nodes'][number]

/**
 * What the saved-view form collects.
 *
 * Derived from the schema input with `Omit` rather than declared, so a field
 * added to `SavedViewCreateInput` appears here automatically and one removed
 * becomes a compile error in this file rather than a property silently
 * ignored on the wire. `workspaceSlug` comes from the URL.
 */
export type SavedViewDraft = Omit<SavedViewCreateInputType, 'workspaceSlug'>

/** The filter half of a draft, in the vocabulary the server accepts. */
export type SavedViewFilterDraft = IssueFilterInputType

/**
 * One entry of a payload's `errors`.
 *
 * Not GraphQL errors: they arrive inside `data` over a successful response.
 * A view the caller may not touch is refused with `code: "NOT_FOUND"` --
 * the same answer a nonexistent id gets, deliberately, so that a refusal
 * cannot be used to probe for what exists.
 */
export type SavedViewValidationError =
  SavedViewCreateMutation['savedViewCreate']['errors'][number]
