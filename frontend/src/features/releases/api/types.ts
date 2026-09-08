/**
 * The names this feature knows the server contract by.
 *
 * Aliases of generated types, every one. Nothing here declares a field, a
 * scalar or a nullability, so nothing here can disagree with the schema.
 */

import type {
  ReleaseCreateMutation,
  ReleaseDetailFieldsFragment,
  ReleaseFieldsFragment,
} from '../../../generated/operations'

export type { ReleaseStatus } from '../../../generated/schema'

/** One release, as a list row draws it. No notes; see ./operations.graphql. */
export type Release = ReleaseFieldsFragment

/** One release with its frozen note and what it shipped. */
export type ReleaseDetail = ReleaseDetailFieldsFragment

/**
 * What the cut-a-release form collects.
 *
 * Declared here rather than aliased to `ReleaseCreateInput`, and the
 * difference is deliberate: `workspaceSlug` is not something a form asks
 * about -- it comes from the URL, in ./mutations.ts -- and including it would
 * let a component supply one.
 *
 * `repositoryId` is a `String` here and an `ID!` in the schema: it is
 * GitHub's own numeric repository id, which arrives from
 * `githubIntegration.repositories` as a string and is passed back untouched.
 * Parsing it to a number here would be inventing a representation the schema
 * does not use.
 */
export interface ReleaseDraft {
  name: string
  environmentId: string
  repositoryId: string
  commitSha: string
  /** Null lets the server resolve the last deploy itself. The normal case. */
  previousCommitSha: string | null
}

/**
 * One entry of a payload's `errors`.
 *
 * A release the caller may not touch is refused with `code: "NOT_FOUND"` --
 * the same answer a nonexistent id gets, deliberately. There is no FORBIDDEN
 * in `PUBLIC_ERROR_CODES`, so this screen never builds a "you do not have
 * permission" state: it cannot know.
 */
export type ReleaseValidationError =
  ReleaseCreateMutation['releaseCreate']['errors'][number]
