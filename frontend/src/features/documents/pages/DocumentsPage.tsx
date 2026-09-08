import { useCallback, useState } from 'react'

import { PageContent, PageHeader } from '../../../app/layout'
import {
  Button,
  Dialog,
  DocumentIcon,
  EmptyState,
  ErrorState,
  List,
  ListRow,
  ListRowMain,
  ListRowMeta,
  Menu,
  Skeleton,
  VisuallyHidden,
} from '../../../components'
import { useInitiativeList } from '../../initiatives/api'
import { useWorkspaceContext } from '../../issues/api'
import { formatRelative } from '../../issues/lib/dates'
import { useDocumentActions, useDocumentDetail, useDocumentList } from '../api'
import type { DocumentDraft, DocumentRow, DocumentValidationError } from '../api'
import { DocumentForm } from '../components/DocumentForm'
import { DocumentPanel } from '../components/DocumentPanel'
import styles from '../documents.module.css'

const NO_ERRORS: readonly DocumentValidationError[] = []

/**
 * Documents: long-form writing that belongs to the workspace, a project or an
 * initiative.
 *
 * ## Why this is one screen and not a list plus a detail route
 *
 * `ROUTE_SEGMENTS` declares `documents` and no `documents/:id`, so there is
 * no per-document URL. Selecting one changes what the panel shows rather than
 * pushing history, and the rows are `<button>`s rather than links -- an `<a>`
 * with nowhere to point breaks middle-click, "open in new tab" and the back
 * button at once. A `documentDetail` segment is the right way to make these
 * addressable, and it belongs to whoever edits the shared path table.
 *
 * ## Where the names come from
 *
 * `creatorId`, `lastEditedBy`, a comment's `authorId`, `projectId` and
 * `initiativeId` are all raw UUIDs; the schema exposes no resolved objects
 * for any of them. People come from the workspace context, projects from the
 * same document's first 50, and initiatives from the initiative list's first
 * 25 -- one query this screen reuses rather than a second copy of it. An id
 * outside those pages is reported as unnameable rather than dropped or
 * guessed at.
 */
export function DocumentsPage() {
  const { members, projects, isLoading: isLoadingContext } = useWorkspaceContext()
  const { initiatives } = useInitiativeList()

  const {
    documents,
    hasNextPage,
    isLoadingFirstPage,
    isLoadingMore,
    errorMessage,
    loadMoreErrorMessage,
    loadMore,
    retry,
  } = useDocumentList()

  const actions = useDocumentActions()

  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [isComposerOpen, setIsComposerOpen] = useState(false)
  const [formErrors, setFormErrors] = useState<readonly DocumentValidationError[]>(NO_ERRORS)
  const [formMessage, setFormMessage] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)

  // The first document until someone chooses otherwise, so the panel has
  // something to show rather than an empty frame beside a full list.
  const selected =
    documents.find((entry) => entry.id === selectedId) ?? documents[0] ?? null

  const detail = useDocumentDetail(selected?.id ?? null)

  const projectById = new Map(projects.map((project) => [project.id, project]))
  const initiativeById = new Map(initiatives.map((entry) => [entry.id, entry]))

  /** What a document hangs off, resolved to a name where one is in hand. */
  const attachmentOf = (document: DocumentRow) => {
    if (document.projectId !== null) {
      return {
        kind: 'project' as const,
        name: projectById.get(document.projectId)?.name ?? null,
      }
    }

    if (document.initiativeId !== null) {
      return {
        kind: 'initiative' as const,
        name: initiativeById.get(document.initiativeId)?.name ?? null,
      }
    }

    return null
  }

  const closeComposer = useCallback(() => {
    setIsComposerOpen(false)
    setFormErrors(NO_ERRORS)
    setFormMessage(null)
  }, [])

  const openComposer = useCallback(() => {
    setFormErrors(NO_ERRORS)
    setFormMessage(null)
    setIsComposerOpen(true)
  }, [])

  /**
   * Report a write that was not made through a form.
   *
   * Above the list rather than beside the control: the row it refers to may
   * have moved by the time it is read.
   */
  const reportOutcome = useCallback(
    (
      outcome: {
        status: string
        errors?: readonly DocumentValidationError[]
        message?: string
      },
      fallback: string,
    ) => {
      if (outcome.status === 'ok') {
        setActionError(null)
        return
      }

      setActionError(
        outcome.status === 'failed'
          ? (outcome.message ?? fallback)
          : (outcome.errors?.[0]?.message ?? fallback),
      )
    },
    [],
  )

  const handleCreate = useCallback(
    (draft: DocumentDraft) => {
      void actions.createDocument(draft).then((outcome) => {
        if (outcome.status === 'ok') {
          closeComposer()
          // Open what was just made: creating a document and being left
          // looking at a different one is the wrong answer to "new".
          setSelectedId(outcome.value.id)
          return
        }

        if (outcome.status === 'rejected') {
          setFormErrors(outcome.errors)
          setFormMessage(null)
          return
        }

        setFormErrors(NO_ERRORS)
        setFormMessage(outcome.message)
      })
    },
    [actions, closeComposer],
  )

  const handleDelete = useCallback(
    (document: DocumentRow) => {
      void actions.deleteDocument(document.id).then((outcome) => {
        if (outcome.status === 'ok') {
          setActionError(null)
          setSelectedId(null)
          return
        }

        reportOutcome(outcome, 'That document could not be deleted.')
      })
    },
    [actions, reportOutcome],
  )

  const handleRename = useCallback(
    (title: string) => {
      if (selected === null) {
        return
      }

      void actions.editDocument(selected.id, { title }).then((outcome) => {
        reportOutcome(outcome, 'That document could not be renamed.')
      })
    },
    [actions, reportOutcome, selected],
  )

  const handleSaveBody = useCallback(
    (content: unknown, snapshot: boolean) => {
      if (selected === null) {
        return
      }

      void actions.editDocument(selected.id, { content, snapshot }).then((outcome) => {
        reportOutcome(outcome, 'That change could not be saved.')
      })
    },
    [actions, reportOutcome, selected],
  )

  const handleRestore = useCallback(
    (revisionId: string) => {
      if (selected === null) {
        return
      }

      void actions.restoreRevision(selected.id, revisionId).then((outcome) => {
        reportOutcome(outcome, 'That version could not be restored.')
      })
    },
    [actions, reportOutcome, selected],
  )

  const handleAddComment = useCallback(
    (body: string) => {
      if (selected === null) {
        return
      }

      void actions.addComment(selected.id, body).then((outcome) => {
        reportOutcome(outcome, 'That comment could not be added.')
      })
    },
    [actions, reportOutcome, selected],
  )

  const handleDeleteComment = useCallback(
    (commentId: string) => {
      void actions.deleteComment(commentId).then((outcome) => {
        reportOutcome(outcome, 'That comment could not be deleted.')
      })
    },
    [actions, reportOutcome],
  )

  const isBusy = isLoadingFirstPage || isLoadingContext

  return (
    <>
      <PageHeader
        title="Documents"
        description="Long-form writing that belongs to the workspace rather than to an issue."
        actions={
          <Button onClick={openComposer} variant="primary">
            New document
          </Button>
        }
      />

      <PageContent>
        {actionError !== null && (
          <p className={styles.actionError} role="alert">
            {actionError}
          </p>
        )}

        {isBusy && (
          <div className={styles.skeletonStack} role="status" aria-busy="true">
            <VisuallyHidden as="div">Loading documents</VisuallyHidden>
            {Array.from({ length: 4 }, (_unused, index) => (
              <Skeleton key={index} width="100%" height="2.75rem" />
            ))}
          </div>
        )}

        {!isBusy && errorMessage !== null && (
          <ErrorState
            title="Could not load documents"
            description={errorMessage}
            onRetry={retry}
          />
        )}

        {!isBusy && errorMessage === null && documents.length === 0 && (
          <EmptyState
            icon={<DocumentIcon />}
            title="No documents yet"
            description="A document is a spec, a brief or a decision record — writing that outlives the issue that prompted it. It can hang off the workspace, a project or an initiative."
            actions={
              <Button onClick={openComposer} variant="primary">
                Write the first document
              </Button>
            }
          />
        )}

        {!isBusy && errorMessage === null && documents.length > 0 && (
          <div className={styles.split}>
            <div className={styles.column}>
              <List label="Documents">
                {documents.map((document) => {
                  const attachment = attachmentOf(document)

                  return (
                    <ListRow
                      interactive
                      key={document.id}
                      selected={document.id === selected?.id}
                    >
                      <ListRowMain>
                        <button
                          aria-current={document.id === selected?.id}
                          className={styles.rowButton}
                          onClick={() => {
                            setSelectedId(document.id)
                          }}
                          type="button"
                        >
                          <span className={styles.rowName}>{document.title}</span>
                          <span className={styles.rowMeta}>
                            <span>
                              {attachment === null
                                ? 'Workspace'
                                : attachment.name === null
                                  ? attachment.kind === 'project'
                                    ? 'A project not on this page'
                                    : 'An initiative not on this page'
                                  : attachment.name}
                            </span>
                            <time dateTime={document.updatedAt}>
                              {formatRelative(document.updatedAt)}
                            </time>
                          </span>
                        </button>
                      </ListRowMain>

                      <ListRowMeta>
                        <Menu
                          align="end"
                          items={[
                            {
                              id: 'delete',
                              label: 'Delete document',
                              destructive: true,
                              onSelect: () => {
                                handleDelete(document)
                              },
                            },
                          ]}
                          label={`Actions on ${document.title}`}
                          size="sm"
                        />
                      </ListRowMeta>
                    </ListRow>
                  )
                })}
              </List>

              {loadMoreErrorMessage !== null && (
                <p className={styles.actionError} role="alert">
                  {loadMoreErrorMessage}
                </p>
              )}

              {hasNextPage && (
                <div className={styles.loadMore}>
                  {/* `DocumentConnection` has no `totalCount`, so "of N" is a
                      number nobody can supply. */}
                  <p className={styles.countLine}>
                    Showing <span className={styles.count}>{documents.length}</span>{' '}
                    documents. There are more.
                  </p>
                  <Button disabled={isLoadingMore} onClick={loadMore} variant="secondary">
                    {isLoadingMore ? 'Loading…' : 'Load more'}
                  </Button>
                </div>
              )}
            </div>

            <div className={styles.column}>
              {detail.errorMessage !== null && (
                <ErrorState
                  title="Could not load this document"
                  description={detail.errorMessage}
                  onRetry={detail.retry}
                />
              )}

              {detail.errorMessage === null && detail.isNotFound && (
                <EmptyState
                  title="This document is no longer available"
                  description="It may have been deleted. Refresh the list to see what is left."
                />
              )}

              {detail.errorMessage === null && detail.isLoading && (
                <div className={styles.skeletonStack} role="status" aria-busy="true">
                  <VisuallyHidden as="div">Loading the document</VisuallyHidden>
                  <Skeleton width="100%" height="14rem" />
                </div>
              )}

              {detail.errorMessage === null &&
                !detail.isLoading &&
                detail.document !== null && (
                  <DocumentPanel
                    attachment={attachmentOf(detail.document)}
                    document={detail.document}
                    isSaving={actions.isSaving}
                    members={members}
                    onAddComment={handleAddComment}
                    onDeleteComment={handleDeleteComment}
                    onRename={handleRename}
                    onRestore={handleRestore}
                    onSaveBody={handleSaveBody}
                  />
                )}
            </div>
          </div>
        )}
      </PageContent>

      <Dialog
        open={isComposerOpen}
        onClose={closeComposer}
        title="New document"
        description="A title, and what it belongs to. The writing comes next."
      >
        <DocumentForm
          errorMessage={formMessage}
          errors={formErrors}
          initiatives={initiatives}
          isSaving={actions.isSaving}
          onCancel={closeComposer}
          onSubmit={handleCreate}
          projects={projects}
        />
      </Dialog>
    </>
  )
}
