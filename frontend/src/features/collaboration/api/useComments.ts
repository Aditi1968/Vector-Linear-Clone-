import { useCallback } from 'react'
import { NetworkStatus } from '@apollo/client'
import { useMutation, useQuery } from '@apollo/client/react'

import { describeError } from '../../issues/lib/errors'
import { appendComment, removeComment } from './cache'
import {
  CommentAuthorsDocument,
  CommentCreateDocument,
  CommentDeleteDocument,
  IssueCommentsDocument,
} from './documents'
import { useLoadMore } from './paging'
import { settle } from './outcome'
import type { MutationOutcome } from './outcome'
import type { Comment } from './types'

/** Stable identity for "no comments", so an empty render is a stable prop. */
const NO_COMMENTS: readonly Comment[] = []

/**
 * How a comment's author is named when the workspace has never heard of them.
 *
 * Reachable, and not a bug: `workspaceMembers` lists who is in the workspace
 * *now*, and a comment outlives its author's membership. Naming them
 * "Unknown" would read as an error; naming them "Former member" says the true
 * thing, which is that the comment is real and the person has gone.
 */
const FORMER_MEMBER = 'Former member'

export interface UseCommentsResult {
  comments: readonly Comment[]
  /** A display name for an author id -- see the note on `FORMER_MEMBER`. */
  authorName: (authorId: string) => string
  /**
   * Whether the reader may withdraw this comment.
   *
   * False whenever `me` is null, which is the unauthenticated case and the
   * pre-authentication development case alike. Offering a control the server
   * would refuse is worse than offering none, and the server is the authority
   * regardless -- this only decides what to draw.
   */
  canDelete: (comment: Comment) => boolean
  isLoading: boolean
  errorMessage: string | null
  retry: () => void
  hasNextPage: boolean
  isLoadingMore: boolean
  loadMoreErrorMessage: string | null
  loadMore: () => void
  postComment: (body: string) => Promise<MutationOutcome<unknown>>
  isPosting: boolean
  deleteComment: (commentId: string) => Promise<MutationOutcome<unknown>>
}

/**
 * One issue's comment thread.
 *
 * The workspace and the issue arrive as arguments rather than from the route,
 * which is the one place this feature departs from `features/issues`. These
 * are panels mounted by a view that already knows both, they can appear more
 * than once on a screen, and a panel that read the URL could not be mounted
 * anywhere else. The ids still have to agree with the URL in practice -- the
 * server scopes every field by `workspaceSlug` -- and the cache keys on the
 * same values, so a panel passed another tenant's slug reads that tenant's
 * cache field and gets the server's refusal, not somebody else's data.
 */
export function useComments(workspaceSlug: string, issueId: string): UseCommentsResult {
  const { data, error, networkStatus, fetchMore, refetch } = useQuery(
    IssueCommentsDocument,
    {
      // `after: null` rather than omitted. The merge policy reads `args.after`
      // to decide whether a result starts the list or extends it, and the
      // cache updates in ./cache.ts read the field back under exactly these
      // variables.
      variables: { workspaceSlug, issueId, after: null },
      notifyOnNetworkStatusChange: true,
    },
  )

  // A separate document: it is a fact about the workspace, not about this
  // issue, so it is shared by every thread the session opens and is not
  // re-sent with each page of comments.
  const { data: authors } = useQuery(CommentAuthorsDocument, {
    variables: { workspaceSlug },
  })

  const connection = data?.issue?.comments
  const comments = connection?.nodes ?? NO_COMMENTS

  const paging = useLoadMore(connection?.pageInfo, networkStatus, (after) =>
    fetchMore({ variables: { after } }),
  )

  const [create, { loading: isPosting }] = useMutation(CommentCreateDocument, {
    // In `update` rather than after the await: this runs inside the
    // mutation's cache transaction, so the new comment and the mutation's own
    // normalised result land in one broadcast and the thread renders once.
    // Doing it afterwards would work, and would flicker.
    update(cache, result) {
      const comment = result.data?.commentCreate.comment

      // Null exactly when the server rejected the body. There is nothing to
      // add, and `errors` is the composer's business, not the cache's.
      if (comment == null) {
        return
      }

      appendComment(cache, workspaceSlug, issueId, comment)
    },
  })

  const [destroy] = useMutation(CommentDeleteDocument, {
    update(cache, result) {
      const deletedCommentId = result.data?.commentDelete.deletedCommentId

      if (deletedCommentId == null) {
        return
      }

      removeComment(cache, workspaceSlug, issueId, deletedCommentId)
    },
  })

  const postComment = useCallback(
    (body: string) =>
      settle(async () => {
        const result = await create({
          variables: { input: { workspaceSlug, issueId, body } },
        })

        return result.data?.commentCreate
      }),
    [create, issueId, workspaceSlug],
  )

  const deleteComment = useCallback(
    (commentId: string) =>
      settle(async () => {
        const result = await destroy({
          variables: { input: { workspaceSlug, id: commentId } },
        })

        return result.data?.commentDelete
      }),
    [destroy, workspaceSlug],
  )

  const viewerId = authors?.me?.id ?? null
  const members = authors?.workspaceMembers

  const authorName = useCallback(
    (authorId: string): string => {
      const member = members?.find((candidate) => candidate.userId === authorId)

      if (member === undefined) {
        return FORMER_MEMBER
      }

      // `name` is nullable on `WorkspaceMember`; the email is always there and
      // is a real way to tell two people apart, which a blank is not.
      return member.name ?? member.email
    },
    [members],
  )

  const canDelete = useCallback(
    (comment: Comment) => viewerId !== null && comment.authorId === viewerId,
    [viewerId],
  )

  const retry = useCallback(() => {
    // Swallowed on purpose: `refetch` rejects *and* sets `error` on the hook
    // result, and `error` is what the panel renders.
    void refetch().catch(() => undefined)
  }, [refetch])

  return {
    comments,
    authorName,
    canDelete,
    // Only the first load. `notifyOnNetworkStatusChange` makes `loading` true
    // during a `fetchMore` as well, and swapping the loaded thread for a
    // skeleton because a later page is arriving is exactly the flicker the
    // separate `isLoadingMore` exists to avoid.
    isLoading: networkStatus === NetworkStatus.loading && connection === undefined,
    errorMessage: error === undefined ? null : describeError(error),
    retry,
    ...paging,
    postComment,
    isPosting,
    deleteComment,
  }
}
