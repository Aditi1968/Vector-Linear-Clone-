import { useState } from 'react'
import { Link } from 'react-router-dom'

import {
  Button,
  EmptyState,
  IconButton,
  CloseIcon,
  PlusIcon,
  RelationIndicator,
  SubIssueIcon,
} from '../../../components'
import { useAppPaths } from '../../../app/routes'
import { describeOutcome, useSubIssues } from '../api'
import type { IssueSummary } from '../api'
import styles from '../collaboration.module.css'
import { IssuePicker } from './IssuePicker'
import { Panel } from './Panel'

export interface SubIssuesPanelProps {
  workspaceSlug: string
  issueId: string
}

/** Which of the two pickers is open. Never both. */
type PickerMode = 'parent' | 'child' | null

/**
 * Where an issue sits in the hierarchy: the one above it, and the ones under
 * it.
 *
 * Both directions live in one panel because they are one fact about the
 * issue, and because they are the same mutation seen from two ends --
 * `issueSetParent` takes the child's id whichever control the user pressed.
 * `useSubIssues` names the four operations so nothing here has to work that
 * out; the two pickers below differ only in which id they hand over.
 */
export function SubIssuesPanel({ workspaceSlug, issueId }: SubIssuesPanelProps) {
  const {
    parent,
    children,
    isLoading,
    errorMessage,
    retry,
    hasNextPage,
    isLoadingMore,
    loadMoreErrorMessage,
    loadMore,
    setParent,
    clearParent,
    addChild,
    removeChildIssue,
    isBusy,
  } = useSubIssues(workspaceSlug, issueId)

  const paths = useAppPaths()
  const [status, setStatus] = useState<string | null>(null)
  const [picker, setPicker] = useState<PickerMode>(null)
  const [pickerError, setPickerError] = useState<string | null>(null)

  const openPicker = (mode: Exclude<PickerMode, null>) => {
    setPickerError(null)
    setPicker(mode)
  }

  const removeChildRow = (child: IssueSummary) => {
    void removeChildIssue(child.id).then((outcome) => {
      setStatus(
        outcome.status === 'ok'
          ? `${child.title} is no longer a sub-issue`
          : describeOutcome(outcome),
      )
    })
  }

  return (
    <Panel
      title="Sub-issues"
      count={isLoading ? undefined : children.length}
      isLoading={isLoading}
      errorMessage={errorMessage}
      onRetry={retry}
      status={status}
      action={
        <Button
          size="sm"
          variant="secondary"
          icon={<PlusIcon />}
          onClick={() => {
            openPicker('child')
          }}
        >
          Add
        </Button>
      }
    >
      <div className={styles.parentRow}>
        <RelationIndicator kind="parent" />
        {parent === null ? (
          <>
            <span className={styles.muted}>No parent issue</span>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => {
                openPicker('parent')
              }}
            >
              Set parent
            </Button>
          </>
        ) : (
          <>
            <Link to={paths.issue(parent.id)} className={styles.rowLink}>
              {parent.title}
            </Link>
            {parent.completedAt !== null && <span className={styles.done}>Done</span>}
            <IconButton
              icon={<CloseIcon />}
              aria-label={`Remove ${parent.title} as the parent issue`}
              disabled={isBusy}
              onClick={() => {
                void clearParent().then((outcome) => {
                  setStatus(
                    outcome.status === 'ok'
                      ? 'Parent issue removed'
                      : describeOutcome(outcome),
                  )
                })
              }}
            />
          </>
        )}
      </div>

      {children.length === 0 ? (
        <EmptyState
          icon={<SubIssueIcon />}
          title="No sub-issues"
          description="Break this issue down into the pieces that finish it."
        />
      ) : (
        <ul className={styles.rows} aria-label="Sub-issues">
          {children.map((child) => (
            <li key={child.id} className={styles.row}>
              <RelationIndicator kind="subIssue" target={child.title} />
              <Link to={paths.issue(child.id)} className={styles.rowLink}>
                {child.title}
              </Link>
              {child.completedAt !== null && <span className={styles.done}>Done</span>}
              <IconButton
                icon={<CloseIcon />}
                aria-label={`Remove ${child.title} as a sub-issue`}
                disabled={isBusy}
                onClick={() => {
                  removeChildRow(child)
                }}
              />
            </li>
          ))}
        </ul>
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
          Load more sub-issues
        </Button>
      )}

      <IssuePicker
        workspaceSlug={workspaceSlug}
        open={picker !== null}
        onClose={() => {
          setPicker(null)
        }}
        title={picker === 'parent' ? 'Choose a parent issue' : 'Add a sub-issue'}
        description={
          picker === 'parent'
            ? 'This issue becomes part of the one you pick.'
            : 'The issue you pick becomes part of this one.'
        }
        errorMessage={pickerError}
        isSubmitting={isBusy}
        // This issue, its current parent and its current children. The server
        // refuses a cycle and a duplicate edge; not offering them is the
        // difference between a picker that cannot fail and one that explains
        // its failures afterwards.
        excludeIds={[
          issueId,
          ...(parent === null ? [] : [parent.id]),
          ...children.map((child) => child.id),
        ]}
        onPick={(issue) => {
          const mode = picker
          const run = mode === 'parent' ? setParent(issue.id) : addChild(issue.id)

          void run.then((outcome) => {
            if (outcome.status !== 'ok') {
              setPickerError(describeOutcome(outcome))
              return
            }

            setPicker(null)
            setPickerError(null)
            setStatus(
              mode === 'parent'
                ? `${issue.identifier} is now the parent issue`
                : `${issue.identifier} added as a sub-issue`,
            )
          })
        }}
      />
    </Panel>
  )
}
