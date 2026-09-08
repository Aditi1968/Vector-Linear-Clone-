/**
 * The label-groups data adapter.
 *
 * The boundary the rest of the feature is written against. The screen imports
 * hooks and types from here and never a document or an Apollo hook. The
 * documents are exported too, and only from this module, because mocking a
 * response requires the exact document that produced it.
 */

export { useLabelGroups } from './queries'
export type { UseLabelGroupsResult } from './queries'

export { useLabelGroupActions } from './mutations'
export type { LabelGroupOutcome, UseLabelGroupActionsResult } from './mutations'

export {
  GroupedLabelListDocument,
  LabelGroupCreateDocument,
  LabelGroupDeleteDocument,
  LabelGroupListDocument,
  LabelGroupUpdateDocument,
  LabelSetGroupDocument,
} from './documents'

export type {
  GroupedLabel,
  LabelGroup,
  LabelGroupDraft,
  LabelGroupValidationError,
} from './types'
