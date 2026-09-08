/**
 * The names this feature knows the server contract by.
 *
 * Aliases of generated types, every one. Nothing here declares a field, a
 * scalar or a nullability, so nothing here can disagree with the schema.
 */

import type {
  DocumentCommentFieldsFragment,
  DocumentCreateMutation,
  DocumentDetailFieldsFragment,
  DocumentRevisionFieldsFragment,
  DocumentRowFieldsFragment,
} from '../../../generated/operations'

/** One document, as a list row draws it. No `content`. */
export type DocumentRow = DocumentRowFieldsFragment

/** One document, whole: content, recent revisions, discussion. */
export type DocumentDetail = DocumentDetailFieldsFragment

/** One stored version, with the text it held. */
export type DocumentRevision = DocumentRevisionFieldsFragment

/** One comment on a document. */
export type DocumentComment = DocumentCommentFieldsFragment

/**
 * What the create form collects.
 *
 * `content` is absent on purpose: a document is created empty and written in
 * afterwards. `DocumentCreateInput.content` defaults to null, which the
 * service turns into the server's own `empty_content()` -- so omitting it
 * says "not written in yet" in the server's words rather than in a client's
 * guess at them.
 */
export interface DocumentDraft {
  title: string
  projectId: string | null
  initiativeId: string | null
}

/**
 * One entry of a payload's `errors`.
 *
 * A document the caller may not touch is refused with `code: "NOT_FOUND"` --
 * the same answer a nonexistent id gets, deliberately. `PUBLIC_ERROR_CODES`
 * has no FORBIDDEN, so this screen never builds a "no permission" state: it
 * cannot know which it was looking at.
 */
export type DocumentValidationError =
  DocumentCreateMutation['documentCreate']['errors'][number]
