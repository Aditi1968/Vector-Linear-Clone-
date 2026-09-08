/**
 * The releases data adapter.
 *
 * The boundary the rest of the feature is written against. The screen imports
 * hooks and types from here and never a document or an Apollo hook. The
 * documents are exported too, and only from this module, because mocking a
 * response requires the exact document that produced it.
 *
 * `useReleaseList` is also read by `features/environments`, which needs the
 * releases in hand to say what was last deployed to each target. That is the
 * same reuse `features/documents` makes of the initiative list: one document,
 * one cache entry, one request.
 */

export { useReleaseDetail, useReleaseList } from './queries'
export type { UseReleaseDetailResult, UseReleaseListResult } from './queries'

export { useReleaseActions } from './mutations'
export type { ReleaseOutcome, UseReleaseActionsResult } from './mutations'

export {
  ReleaseCreateDocument,
  ReleaseDeleteDocument,
  ReleaseDetailDocument,
  ReleaseListDocument,
  ReleaseStatusSetDocument,
} from './documents'

export type {
  Release,
  ReleaseDetail,
  ReleaseDraft,
  ReleaseStatus,
  ReleaseValidationError,
} from './types'
