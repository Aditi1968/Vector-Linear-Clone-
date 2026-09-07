/**
 * The templates data adapter.
 *
 * The boundary the rest of the feature is written against. The screen imports
 * hooks and types from here and never a document or an Apollo hook. The
 * documents are exported too, and only from this module, because mocking a
 * response requires the exact document that produced it.
 */

export { useTemplateList } from './queries'
export type { UseTemplateListResult } from './queries'

export { useTemplateActions } from './mutations'
export type { TemplateOutcome, UseTemplateActionsResult } from './mutations'

export {
  IssueCreateFromTemplateDocument,
  IssueTemplateCreateDocument,
  IssueTemplateDeleteDocument,
  IssueTemplateListDocument,
  IssueTemplateUpdateDocument,
} from './documents'

export type {
  CreatedIssue,
  IssueTemplate,
  IssueTemplateDraft,
  IssueTemplateFieldsInput,
  TemplateValidationError,
} from './types'
