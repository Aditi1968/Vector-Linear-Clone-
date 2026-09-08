import { useEffect, useId, useState } from 'react'

import {
  Button,
  Checkbox,
  Input,
  InspectorPanel,
  Skeleton,
  Tabs,
  Textarea,
  VisuallyHidden,
} from '../../../components'
import { memberLabel } from '../../issues/api'
import type { WorkspaceMember } from '../../issues/api'
import { formatAbsolute, formatRelative } from '../../issues/lib/dates'
import type { DocumentDetail } from '../api'
import {
  fromPlainText,
  isPlainTextDocument,
  readDocument,
  toPlainText,
} from '../lib/content'
import { DocumentBody } from './DocumentBody'
import styles from '../documents.module.css'

export interface DocumentPanelProps {
  document: DocumentDetail
  members: readonly WorkspaceMember[]
  /** What the document is attached to, already resolved to a name or null. */
  attachment: { kind: 'project' | 'initiative'; name: string | null } | null
  isSaving: boolean
  onRename: (title: string) => void
  onSaveBody: (content: unknown, snapshot: boolean) => void
  onRestore: (revisionId: string) => void
  onAddComment: (body: string) => void
  onDeleteComment: (commentId: string) => void
}

/**
 * One open document: its text, its history, its discussion.
 *
 * ## Why the body editor is a textarea, and when it refuses to open
 *
 * `Document.content` is a ProseMirror tree with a closed vocabulary --
 * headings, lists, quotes, code blocks, and five marks. A textarea can carry
 * paragraphs and line breaks and nothing else, so editing a formatted
 * document through one would return it stripped: the same class of bug as a
 * whole-row-replace form clearing the fields it did not render, and just as
 * invisible until somebody looks at what used to be a list.
 *
 * So the editor asks first. `isPlainTextDocument` decides, and a document it
 * refuses is rendered and left read-only with the reason on screen. The title
 * stays editable in both cases -- `DocumentEditInput` is a patch, and a
 * rename sends no content at all.
 *
 * ponytail: the ceiling is the textarea, and the upgrade is a rich editor
 * bound to the same vocabulary (TipTap, whose document shape this already
 * is). Until then the screen would rather refuse an edit than lose a
 * paragraph.
 */
export function DocumentPanel({
  document,
  members,
  attachment,
  isSaving,
  onRename,
  onSaveBody,
  onRestore,
  onAddComment,
  onDeleteComment,
}: DocumentPanelProps) {
  const fieldId = useId()

  const [tab, setTab] = useState('body')
  const [title, setTitle] = useState(document.title)
  const [isEditingBody, setIsEditingBody] = useState(false)
  const [draft, setDraft] = useState('')
  const [snapshot, setSnapshot] = useState(false)
  const [comment, setComment] = useState('')

  /*
    Reset the local edits when a different document is opened. Without this,
    selecting a second document would show the first one's title in the box
    and offer to save it over the second -- the panel is one component
    instance reused for every row, so its state does not reset on its own.
  */
  useEffect(() => {
    setTitle(document.title)
    setIsEditingBody(false)
    setDraft('')
    setSnapshot(false)
    setComment('')
    setTab('body')
  }, [document.id, document.title])

  const memberById = new Map(members.map((member) => [member.userId, member]))
  const nodes = readDocument(document.content)
  const isEditable = nodes !== null && isPlainTextDocument(nodes)

  const nameOf = (userId: string) => {
    const member = memberById.get(userId)

    return member === undefined ? null : memberLabel(member)
  }

  const bodyTab = (
    <>
      {nodes === null && (
        <p className={styles.note}>
          This document&rsquo;s body is not in a shape Vector can read, so it is not shown.
          Nothing has been changed; the stored content is still there.
        </p>
      )}

      {nodes !== null && !isEditingBody && (
        <>
          {nodes.length === 0 ? (
            <p className={styles.note}>This document is empty.</p>
          ) : (
            <DocumentBody nodes={nodes} />
          )}

          {isEditable ? (
            <div className={styles.bodyActions}>
              <Button
                onClick={() => {
                  setDraft(toPlainText(nodes))
                  setIsEditingBody(true)
                }}
                type="button"
                variant="secondary"
              >
                Edit body
              </Button>
            </div>
          ) : (
            <p className={styles.note}>
              This document uses headings, lists or formatting that Vector&rsquo;s
              plain-text editor cannot carry, so editing the body here is turned off
              rather than risk stripping it. The title can still be changed.
            </p>
          )}
        </>
      )}

      {nodes !== null && isEditingBody && (
        <div className={styles.editor}>
          <div className={styles.field}>
            <label className={styles.label} htmlFor={`${fieldId}-body`}>
              Document body
            </label>
            <Textarea
              className={styles.bodyInput}
              id={`${fieldId}-body`}
              onChange={(event) => {
                setDraft(event.target.value)
              }}
              rows={16}
              value={draft}
            />
            <p className={styles.hint}>
              A blank line starts a new paragraph. A single line break stays inside one.
            </p>
          </div>

          <div className={styles.checkboxRow}>
            <Checkbox
              checked={snapshot}
              id={`${fieldId}-snapshot`}
              onChange={(event) => {
                setSnapshot(event.target.checked)
              }}
            />
            <label className={styles.checkboxLabel} htmlFor={`${fieldId}-snapshot`}>
              Keep the current text as a version I can come back to
            </label>
          </div>

          <div className={styles.formActions}>
            <Button
              onClick={() => {
                setIsEditingBody(false)
              }}
              type="button"
              variant="ghost"
            >
              Cancel
            </Button>
            <Button
              disabled={isSaving}
              onClick={() => {
                onSaveBody(fromPlainText(draft), snapshot)
                setIsEditingBody(false)
              }}
              type="button"
              variant="primary"
            >
              Save body
            </Button>
          </div>
        </div>
      )}
    </>
  )

  const historyTab = (
    <>
      <p className={styles.note}>
        A version is kept when somebody else edits after you, when an edit follows a gap
        of ten minutes, and whenever an author asks for one. Restoring is itself an edit,
        so the text being replaced becomes a version of its own.
      </p>

      {document.revisions.nodes.length === 0 && (
        <p className={styles.note}>No earlier versions yet.</p>
      )}

      <ol className={styles.revisions}>
        {document.revisions.nodes.map((revision) => {
          const author = nameOf(revision.authorId)
          const revisionNodes = readDocument(revision.content)
          const stamp = formatAbsolute(revision.createdAt)

          return (
            <li className={styles.revision} key={revision.id}>
              <div className={styles.revisionHead}>
                <span className={styles.revisionTitle}>{revision.title}</span>
                <span className={styles.revisionMeta}>
                  {author ?? <span className={styles.unresolved}>Someone not in this list</span>}
                  {' · '}
                  <time dateTime={revision.createdAt}>
                    {formatRelative(revision.createdAt)}
                  </time>
                </span>
              </div>

              {/* Native disclosure: a summary and a body is exactly what
                  `<details>` is, and it needs no state, no aria wiring and no
                  keyboard handling of its own. */}
              <details className={styles.revisionPreview}>
                <summary>{`Preview the version from ${stamp}`}</summary>
                {revisionNodes === null ? (
                  <p className={styles.note}>
                    This version&rsquo;s body is not in a shape Vector can read.
                  </p>
                ) : (
                  <DocumentBody nodes={revisionNodes} />
                )}
              </details>

              <Button
                disabled={isSaving}
                onClick={() => {
                  onRestore(revision.id)
                }}
                size="sm"
                type="button"
                variant="secondary"
              >
                {`Restore the version from ${stamp}`}
              </Button>
            </li>
          )
        })}
      </ol>

      {document.revisions.pageInfo.hasNextPage && (
        <p className={styles.note}>
          Older versions exist beyond these. This panel shows the five most recent.
        </p>
      )}
    </>
  )

  const commentsTab = (
    <>
      <div className={styles.field}>
        <label className={styles.label} htmlFor={`${fieldId}-comment`}>
          Add a comment
        </label>
        <Textarea
          id={`${fieldId}-comment`}
          onChange={(event) => {
            setComment(event.target.value)
          }}
          rows={3}
          value={comment}
        />
      </div>

      <div className={styles.formActions}>
        <Button
          disabled={isSaving || comment.trim() === ''}
          onClick={() => {
            onAddComment(comment.trim())
            setComment('')
          }}
          type="button"
          variant="primary"
        >
          Comment
        </Button>
      </div>

      {document.comments.nodes.length === 0 && (
        <p className={styles.note}>Nothing has been said about this document yet.</p>
      )}

      <ol className={styles.comments}>
        {document.comments.nodes.map((entry) => {
          const author = nameOf(entry.authorId)
          const stamp = formatAbsolute(entry.createdAt)

          return (
            <li className={styles.comment} key={entry.id}>
              <div className={styles.commentHead}>
                <span className={styles.commentAuthor}>
                  {author ?? <span className={styles.unresolved}>Someone not in this list</span>}
                </span>
                <span className={styles.commentMeta}>
                  <time dateTime={entry.createdAt}>{formatRelative(entry.createdAt)}</time>
                  {/* An edited comment says so. The schema carries
                      `editedAt` separately from `updatedAt` precisely so a
                      reader can tell a rewritten comment from an untouched
                      one. */}
                  {entry.editedAt !== null && ' · edited'}
                </span>
              </div>

              <p className={styles.commentBody}>{entry.body}</p>

              <Button
                disabled={isSaving}
                onClick={() => {
                  onDeleteComment(entry.id)
                }}
                size="sm"
                type="button"
                variant="ghost"
              >
                <VisuallyHidden>{`Delete the comment from ${stamp}`}</VisuallyHidden>
                <span aria-hidden="true">Delete</span>
              </Button>
            </li>
          )
        })}
      </ol>

      {document.comments.pageInfo.hasNextPage && (
        <p className={styles.note}>
          There are more comments than these. This panel shows the first twenty.
        </p>
      )}
    </>
  )

  return (
    <InspectorPanel
      label={`Document ${document.title}`}
      header={
        <div className={styles.panelHeader}>
          <h2 className={styles.panelTitle}>{document.title}</h2>
          <p className={styles.panelMeta}>
            {attachment === null ? (
              'A workspace document'
            ) : attachment.name === null ? (
              <span className={styles.unresolved}>
                {attachment.kind === 'project'
                  ? 'In a project this screen cannot name'
                  : 'In an initiative this screen cannot name'}
              </span>
            ) : (
              `${attachment.kind === 'project' ? 'Project' : 'Initiative'} · ${attachment.name}`
            )}
            {' · last edited '}
            <time dateTime={document.updatedAt}>{formatRelative(document.updatedAt)}</time>
            {' by '}
            {nameOf(document.lastEditedBy) ?? (
              <span className={styles.unresolved}>someone not in this list</span>
            )}
          </p>
        </div>
      }
    >
      <div className={styles.titleRow}>
        <div className={styles.field}>
          <label className={styles.label} htmlFor={`${fieldId}-title`}>
            Document title
          </label>
          <Input
            id={`${fieldId}-title`}
            onChange={(event) => {
              setTitle(event.target.value)
            }}
            value={title}
          />
        </div>
        <Button
          disabled={isSaving || title.trim() === '' || title === document.title}
          onClick={() => {
            onRename(title.trim())
          }}
          type="button"
        >
          Rename
        </Button>
      </div>

      {isSaving && (
        <div className={styles.savingRow} role="status" aria-busy="true">
          <VisuallyHidden as="div">Saving</VisuallyHidden>
          <Skeleton width="100%" height="0.25rem" />
        </div>
      )}

      <Tabs
        label="Document, history and comments"
        onChange={setTab}
        tabs={[
          { id: 'body', label: 'Document', content: bodyTab },
          {
            id: 'history',
            label: 'History',
            count: document.revisions.nodes.length,
            content: historyTab,
          },
          {
            id: 'comments',
            label: 'Comments',
            count: document.comments.nodes.length,
            content: commentsTab,
          },
        ]}
        value={tab}
      />
    </InspectorPanel>
  )
}
