import { useId, useState } from 'react'

import {
  Avatar,
  Button,
  Dialog,
  EmptyState,
  IconButton,
  CloseIcon,
  CommentIcon,
  Textarea,
} from '../../../components'
import { formatAbsolute, formatRelative } from '../../issues/lib/dates'
import { describeOutcome, useComments } from '../api'
import type { Comment } from '../api'
import styles from '../collaboration.module.css'
import { Panel } from './Panel'

export interface CommentsPanelProps {
  /** The workspace every request this panel makes is scoped to. */
  workspaceSlug: string
  /** The issue whose thread this is. */
  issueId: string
}

/**
 * An issue's discussion.
 *
 * ## There is no edit
 *
 * The schema has `commentCreate` and `commentDelete` and nothing else.
 * `Comment.editedAt` exists on the type and nothing on the server ever sets
 * it, so this panel neither offers an edit control nor renders an "edited"
 * marker: both would tell the user the product does something it does not.
 * If an edit mutation lands, the marker belongs here at the same time.
 *
 * ## Delete is confirmed; the rest of this feature is not
 *
 * Withdrawing a comment cannot be undone -- there is no restore mutation and
 * no draft left behind -- so it goes through a dialog. Detaching a label or
 * removing a relation is one click to reverse, and putting a modal in front
 * of a reversible action trains people to dismiss modals.
 */
export function CommentsPanel({ workspaceSlug, issueId }: CommentsPanelProps) {
  const {
    comments,
    authorName,
    canDelete,
    isLoading,
    errorMessage,
    retry,
    hasNextPage,
    isLoadingMore,
    loadMoreErrorMessage,
    loadMore,
    postComment,
    isPosting,
    deleteComment,
  } = useComments(workspaceSlug, issueId)

  const [body, setBody] = useState('')
  const [formError, setFormError] = useState<string | null>(null)
  const [status, setStatus] = useState<string | null>(null)
  const [pendingDelete, setPendingDelete] = useState<Comment | null>(null)
  const [isDeleting, setIsDeleting] = useState(false)
  const composerId = useId()

  const isSubmittable = body.trim().length > 0 && !isPosting

  const submit = async () => {
    if (!isSubmittable) {
      return
    }

    setFormError(null)
    const outcome = await postComment(body.trim())

    if (outcome.status !== 'ok') {
      setFormError(describeOutcome(outcome))
      return
    }

    // Cleared only on success. A failed post that emptied the box would lose
    // what the person wrote, and the most likely cause of the failure -- a
    // dropped connection -- is also the one where they most want it back.
    setBody('')
    setStatus('Comment posted')
  }

  const confirmDelete = async () => {
    if (pendingDelete === null) {
      return
    }

    setIsDeleting(true)
    const outcome = await deleteComment(pendingDelete.id)
    setIsDeleting(false)
    setPendingDelete(null)

    setStatus(outcome.status === 'ok' ? 'Comment deleted' : describeOutcome(outcome))
  }

  return (
    <Panel
      title="Comments"
      count={isLoading ? undefined : comments.length}
      isLoading={isLoading}
      errorMessage={errorMessage}
      onRetry={retry}
      status={status}
    >
      {comments.length === 0 ? (
        <EmptyState
          icon={<CommentIcon />}
          title="No comments yet"
          description="Be the first to say something about this issue."
        />
      ) : (
        <ol className={styles.thread} aria-label="Comments on this issue">
          {comments.map((comment) => {
            const name = authorName(comment.authorId)

            return (
              <li key={comment.id} className={styles.comment}>
                <Avatar name={name} size="sm" decorative />
                <div className={styles.commentBody}>
                  <div className={styles.commentMeta}>
                    <span className={styles.commentAuthor}>{name}</span>
                    {/* A machine-readable instant with a human one on top: the
                      * relative form is what people read, the `title` is what
                      * they check when "2 months ago" is not precise enough. */}
                    <time
                      dateTime={comment.createdAt}
                      title={formatAbsolute(comment.createdAt)}
                      className={styles.commentTime}
                    >
                      {formatRelative(comment.createdAt)}
                    </time>
                    {canDelete(comment) && (
                      <IconButton
                        icon={<CloseIcon />}
                        aria-label={`Delete comment by ${name}`}
                        className={styles.commentDelete}
                        onClick={() => {
                          setPendingDelete(comment)
                        }}
                      />
                    )}
                  </div>
                  {/* `white-space: pre-wrap` in the stylesheet, so the line
                    * breaks someone typed survive without this having to
                    * render markup from user input. */}
                  <p className={styles.commentText}>{comment.body}</p>
                </div>
              </li>
            )
          })}
        </ol>
      )}

      {loadMoreErrorMessage !== null && (
        <p role="alert" className={styles.formError}>
          {loadMoreErrorMessage}
        </p>
      )}

      {hasNextPage && (
        <Button
          variant="secondary"
          size="sm"
          loading={isLoadingMore}
          onClick={loadMore}
          className={styles.loadMore}
        >
          Load more comments
        </Button>
      )}

      <form
        className={styles.composer}
        onSubmit={(event) => {
          event.preventDefault()
          void submit()
        }}
      >
        <label htmlFor={composerId} className={styles.fieldLabel}>
          Add a comment
        </label>
        <Textarea
          id={composerId}
          value={body}
          rows={3}
          placeholder="Leave a comment"
          invalid={formError !== null}
          aria-describedby={formError === null ? undefined : `${composerId}-error`}
          onChange={(event) => {
            setBody(event.target.value)
            setFormError(null)
          }}
        />
        {formError !== null && (
          <p id={`${composerId}-error`} role="alert" className={styles.formError}>
            {formError}
          </p>
        )}
        <div className={styles.composerActions}>
          <Button type="submit" size="sm" disabled={!isSubmittable} loading={isPosting}>
            Comment
          </Button>
        </div>
      </form>

      <Dialog
        open={pendingDelete !== null}
        onClose={() => {
          setPendingDelete(null)
        }}
        title="Delete this comment?"
        description="This cannot be undone."
        size="sm"
        footer={
          <>
            <Button
              variant="secondary"
              onClick={() => {
                setPendingDelete(null)
              }}
            >
              Cancel
            </Button>
            <Button
              variant="danger"
              loading={isDeleting}
              onClick={() => {
                void confirmDelete()
              }}
            >
              Delete comment
            </Button>
          </>
        }
      >
        <p className={styles.confirmBody}>{pendingDelete?.body}</p>
      </Dialog>
    </Panel>
  )
}
