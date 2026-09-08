import { useCallback } from 'react'
import { useMutation } from '@apollo/client/react'

import { useWorkspaceSlug } from '../../../app/routes'
import { readPayload } from '../../../lib/graphql'
import type { PayloadOutcome } from '../../../lib/graphql'
import { describeError } from '../../issues/lib/errors'
import {
  DocumentCommentCreateDocument,
  DocumentCommentDeleteDocument,
  DocumentCreateDocument,
  DocumentDeleteDocument,
  DocumentEditDocument,
  DocumentRestoreDocument,
} from './documents'
import type {
  DocumentComment,
  DocumentDetail,
  DocumentDraft,
  DocumentValidationError,
} from './types'

/**
 * What a document write can do, as three cases that cannot be confused.
 *
 * See `src/lib/graphql/payload.ts` for the reader and for why `rejected` and
 * `failed` are not one case.
 */
export type DocumentOutcome<T> = PayloadOutcome<T, DocumentValidationError>

/**
 * An edit, as a patch.
 *
 * `DocumentEditInput` leaves a field alone when it is absent, so a rename
 * sends `title` and no `content`, and a body save sends `content` and no
 * `title`. Sending both when only one changed is not wrong -- the service
 * compares the stored content and does nothing when it matches -- but it does
 * make an edit look like two.
 *
 * `snapshot` forces the previous text into the history before the new text
 * lands. The server snapshots on its own when the author changes or ten
 * minutes have passed (`REVISION_GAP`); this is for the boundary only the
 * author can see.
 */
export interface DocumentEditPatch {
  title?: string
  content?: unknown
  snapshot?: boolean
}

export interface UseDocumentActionsResult {
  createDocument: (draft: DocumentDraft) => Promise<DocumentOutcome<DocumentDetail>>
  editDocument: (
    id: string,
    patch: DocumentEditPatch,
  ) => Promise<DocumentOutcome<DocumentDetail>>
  restoreRevision: (
    documentId: string,
    revisionId: string,
  ) => Promise<DocumentOutcome<DocumentDetail>>
  deleteDocument: (id: string) => Promise<DocumentOutcome<string>>
  addComment: (
    documentId: string,
    body: string,
  ) => Promise<DocumentOutcome<DocumentComment>>
  deleteComment: (id: string) => Promise<DocumentOutcome<string>>
  isSaving: boolean
}

/**
 * Every write the documents screen makes.
 *
 * ## Which of these needs cache help
 *
 * `documentEdit` and `documentRestore` both return the whole detail fragment
 * over the same `Document:<uuid>` the list holds, so the row's title and the
 * open document's history both correct themselves by normalisation. The
 * `revisions` and `comments` connections hanging off that entity have field
 * policies in `src/lib/graphql/cache.ts`; without them Apollo would replace
 * the stored connection object wholesale and warn that cache data may be lost.
 *
 * Create and delete change the membership of the `documents` connection,
 * which neither payload is part of, so both refetch the list.
 *
 * The two comment mutations refetch the open document. The create payload
 * holds the new comment but not the connection it belongs to; the delete
 * payload holds only an id. Writing either into `Document.comments` by hand
 * would be this feature reimplementing the merge the cache already owns.
 */
export function useDocumentActions(): UseDocumentActionsResult {
  const workspaceSlug = useWorkspaceSlug()

  const [create, createState] = useMutation(DocumentCreateDocument, {
    refetchQueries: ['DocumentList'],
  })
  const [edit, editState] = useMutation(DocumentEditDocument)
  const [restore, restoreState] = useMutation(DocumentRestoreDocument)
  const [remove, removeState] = useMutation(DocumentDeleteDocument, {
    refetchQueries: ['DocumentList'],
  })
  const [commentCreate, commentCreateState] = useMutation(DocumentCommentCreateDocument, {
    refetchQueries: ['DocumentDetail'],
  })
  const [commentDelete, commentDeleteState] = useMutation(DocumentCommentDeleteDocument, {
    refetchQueries: ['DocumentDetail'],
  })

  const createDocument = useCallback(
    async (draft: DocumentDraft) => {
      try {
        const result = await create({ variables: { input: { workspaceSlug, ...draft } } })

        return readPayload(
          result.data?.documentCreate,
          result.data?.documentCreate.document,
        )
      } catch (reason) {
        // The default `errorPolicy` of `none` makes `mutate` reject on a
        // top-level GraphQL error as well as on a transport failure.
        return { status: 'failed' as const, message: describeError(reason) }
      }
    },
    [create, workspaceSlug],
  )

  const editDocument = useCallback(
    async (id: string, patch: DocumentEditPatch) => {
      try {
        const result = await edit({
          variables: {
            input: {
              workspaceSlug,
              id,
              // `snapshot` is non-null in the input with a default of false,
              // so it is always sent rather than spread conditionally.
              snapshot: patch.snapshot ?? false,
              ...(patch.title === undefined ? {} : { title: patch.title }),
              ...(patch.content === undefined ? {} : { content: patch.content }),
            },
          },
        })

        return readPayload(result.data?.documentEdit, result.data?.documentEdit.document)
      } catch (reason) {
        return { status: 'failed' as const, message: describeError(reason) }
      }
    },
    [edit, workspaceSlug],
  )

  const restoreRevision = useCallback(
    async (documentId: string, revisionId: string) => {
      try {
        const result = await restore({
          variables: { input: { workspaceSlug, documentId, revisionId } },
        })

        return readPayload(
          result.data?.documentRestore,
          result.data?.documentRestore.document,
        )
      } catch (reason) {
        return { status: 'failed' as const, message: describeError(reason) }
      }
    },
    [restore, workspaceSlug],
  )

  const deleteDocument = useCallback(
    async (id: string) => {
      try {
        const result = await remove({ variables: { input: { workspaceSlug, id } } })
        const payload = result.data?.documentDelete

        return readPayload(payload, payload?.deletedDocumentId)
      } catch (reason) {
        return { status: 'failed' as const, message: describeError(reason) }
      }
    },
    [remove, workspaceSlug],
  )

  const addComment = useCallback(
    async (documentId: string, body: string) => {
      try {
        const result = await commentCreate({
          variables: { input: { workspaceSlug, documentId, body } },
        })

        return readPayload(
          result.data?.documentCommentCreate,
          result.data?.documentCommentCreate.comment,
        )
      } catch (reason) {
        return { status: 'failed' as const, message: describeError(reason) }
      }
    },
    [commentCreate, workspaceSlug],
  )

  const deleteComment = useCallback(
    async (id: string) => {
      try {
        const result = await commentDelete({ variables: { input: { workspaceSlug, id } } })
        const payload = result.data?.documentCommentDelete

        return readPayload(payload, payload?.deletedCommentId)
      } catch (reason) {
        return { status: 'failed' as const, message: describeError(reason) }
      }
    },
    [commentDelete, workspaceSlug],
  )

  return {
    createDocument,
    editDocument,
    restoreRevision,
    deleteDocument,
    addComment,
    deleteComment,
    isSaving:
      createState.loading ||
      editState.loading ||
      restoreState.loading ||
      removeState.loading ||
      commentCreateState.loading ||
      commentDeleteState.loading,
  }
}
