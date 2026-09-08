/**
 * The environments data adapter.
 *
 * The boundary the rest of the feature is written against. The screen imports
 * hooks and types from here and never a document or an Apollo hook. The
 * documents are exported too, and only from this module, because mocking a
 * response requires the exact document that produced it.
 *
 * `useEnvironmentList` is also read by `features/releases`: a release names an
 * `environmentId` and the schema exposes no `Release.environment`, so the only
 * place a target's name comes from is this list.
 */

export { useEnvironmentList } from './queries'
export type { UseEnvironmentListResult } from './queries'

export { useEnvironmentActions } from './mutations'
export type { EnvironmentOutcome, UseEnvironmentActionsResult } from './mutations'

export { EnvironmentCreateDocument, EnvironmentListDocument } from './documents'

export type {
  Environment,
  EnvironmentDraft,
  EnvironmentKind,
  EnvironmentValidationError,
} from './types'
