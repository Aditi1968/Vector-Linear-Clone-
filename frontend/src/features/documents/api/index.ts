/**
 * The documents data adapter.
 *
 * The boundary the rest of the feature is written against. The screen imports
 * hooks and types from here and never a GraphQL document or an Apollo hook.
 * The documents are exported too, and only from this module, because mocking
 * a response requires the exact document that produced it.
 */

export { useDocumentDetail, useDocumentList } from './queries'
export type { UseDocumentDetailResult, UseDocumentListResult } from './queries'

export { useDocumentActions } from './mutations'
export type {
  DocumentEditPatch,
  DocumentOutcome,
  UseDocumentActionsResult,
} from './mutations'

export {
  DocumentCommentCreateDocument,
  DocumentCommentDeleteDocument,
  DocumentCreateDocument,
  DocumentDeleteDocument,
  DocumentDetailDocument,
  DocumentEditDocument,
  DocumentListDocument,
  DocumentRestoreDocument,
} from './documents'

export type {
  DocumentComment,
  DocumentDetail,
  DocumentDraft,
  DocumentRevision,
  DocumentRow,
  DocumentValidationError,
} from './types'
