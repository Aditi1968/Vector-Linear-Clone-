/**
 * The initiatives data adapter.
 *
 * The boundary the rest of the feature is written against. The screen imports
 * hooks and types from here and never a document or an Apollo hook. The
 * documents are exported too, and only from this module, because mocking a
 * response requires the exact document that produced it.
 */

export { useInitiativeDetail, useInitiativeList } from './queries'
export type { UseInitiativeDetailResult, UseInitiativeListResult } from './queries'

export { useInitiativeActions } from './mutations'
export type {
  InitiativeOutcome,
  InitiativePatch,
  UseInitiativeActionsResult,
} from './mutations'

export {
  InitiativeClearParentDocument,
  InitiativeCreateDocument,
  InitiativeDeleteDocument,
  InitiativeDetailDocument,
  InitiativeListDocument,
  InitiativeProjectAddDocument,
  InitiativeProjectRemoveDocument,
  InitiativeSetParentDocument,
  InitiativeUpdateDocument,
  InitiativeUpdatePostDocument,
} from './documents'

export type {
  Health,
  Initiative,
  InitiativeDetail,
  InitiativeDraft,
  InitiativeStatus,
  InitiativeUpdateEntry,
  InitiativeValidationError,
} from './types'
