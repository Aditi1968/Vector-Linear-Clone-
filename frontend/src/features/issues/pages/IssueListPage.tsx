import { useCallback, useEffect, useState } from 'react'

import {
  PageContent,
  PageHeader,
  useRegisterCreateIssueAction,
} from '../../../app/layout'
import { Button } from '../../../components'
import { useIssueList } from '../api'
import { IssueComposer } from '../components/IssueComposer'
import { IssueRow } from '../components/IssueRow'
import {
  IssueListEmpty,
  IssueListSkeleton,
  IssueLoadError,
} from '../components/ListStates'
import styles from '../issues.module.css'
import { isTypingTarget } from '../lib/keyboard'

/**
 * The issue list.
 *
 * ## Shell integration
 *
 * `AppLayout` owns the `<main>` landmark and `PageHeader` owns the page's
 * only `<h1>`, so this screen renders neither: a nested `main` produces two
 * "main" landmarks and defeats the landmark navigation the shell is built
 * around.
 *
 * The composer is offered to the shell through
 * `useRegisterCreateIssueAction`, which is what makes the sidebar's "New
 * issue" button work. There is deliberately no second "New issue" button in
 * the page header -- the sidebar already has one in view, and two identical
 * primary buttons a few hundred pixels apart read as a bug. What the header
 * carries instead is the keyboard hint, which is an affordance the sidebar
 * does not advertise.
 *
 * ## Pagination
 *
 * Cursor-based, and there is no number anywhere in this file that could be an
 * offset. "Load more" sends `pageInfo.endCursor` as `after`; the cache's field
 * policy merges the page onto the ones already loaded. Rows on screen stay on
 * screen while the next page is in flight, because nothing here clears them
 * and the merge policy only ever adds.
 *
 * ## Why the footer is a chain of cases and not a button with a spinner
 *
 * The bottom of a paginated list has four states and they are not variations
 * of one another: fetching the next page, having failed to fetch it, having
 * more to fetch, and having reached the end. The last is the one that usually
 * gets skipped, leaving a "Load more" button that does nothing -- so
 * `hasNextPage: false` renders a statement about the end of the list and no
 * button at all.
 *
 * ## Keyboard
 *
 * One `keydown` listener, added while this screen is mounted and removed with
 * it. That is the whole keyboard implementation: no registry, no provider, no
 * key-sequence machinery.
 */
export function IssueListPage() {
  const {
    issues,
    hasNextPage,
    isLoadingFirstPage,
    isLoadingMore,
    isRefreshing,
    errorMessage,
    loadMoreErrorMessage,
    loadMore,
    retry,
  } = useIssueList()

  const [isComposerOpen, setIsComposerOpen] = useState(false)

  const openComposer = useCallback(() => {
    setIsComposerOpen(true)
  }, [])

  const closeComposer = useCallback(() => {
    setIsComposerOpen(false)
  }, [])

  // Fills the shell's create-issue slot. A no-op outside the shell, which is
  // what keeps this screen mountable on its own in a test.
  useRegisterCreateIssueAction(openComposer)

  const handleCreated = useCallback(() => {
    // No navigation and no reload. The mutation's cache update has already
    // put the new row at the head of the list, so closing the composer
    // reveals it in place -- which is also the most direct evidence that the
    // cache update worked.
    setIsComposerOpen(false)
  }, [])

  useEffect(() => {
    function handleKeyDown(event: KeyboardEvent) {
      // Something nearer the event already dealt with it.
      if (event.defaultPrevented) {
        return
      }

      if (event.key === 'Escape') {
        // Not subject to the typing check below, and that asymmetry is the
        // point: Escape while typing in the composer is exactly the case it
        // has to handle. Escape with nothing open is left alone so it can
        // still dismiss whatever the browser or a future dialog owns.
        if (!isComposerOpen) {
          return
        }

        event.preventDefault()
        closeComposer()
        return
      }

      if (event.key !== 'c' && event.key !== 'C') {
        return
      }

      // Ctrl+C, Cmd+C and Alt+C belong to the platform. Only a bare `c`
      // (with or without Shift, which is how a capital C arrives) is ours.
      if (event.ctrlKey || event.metaKey || event.altKey) {
        return
      }

      if (isComposerOpen) {
        return
      }

      // The guard that makes a single-letter shortcut safe: typing the word
      // "critical" into the title field must not open a second composer.
      if (isTypingTarget(event.target)) {
        return
      }

      event.preventDefault()
      openComposer()
    }

    window.addEventListener('keydown', handleKeyDown)

    return () => {
      window.removeEventListener('keydown', handleKeyDown)
    }
  }, [closeComposer, isComposerOpen, openComposer])

  const hasIssues = issues.length > 0
  const isEmpty = !isLoadingFirstPage && errorMessage === null && !hasIssues

  return (
    <>
      <PageHeader
        title="Issues"
        actions={
          <span className={styles.fieldHint}>
            Press <kbd className={styles.shortcutHint}>C</kbd> to create
          </span>
        }
      />

      <PageContent>
        {isComposerOpen && (
          <IssueComposer onCancel={closeComposer} onCreated={handleCreated} />
        )}

        {isLoadingFirstPage && <IssueListSkeleton />}

        {/*
          The whole-list error panel replaces the list only when there is no
          list to replace. If rows are already loaded and a later request
          fails, the rows stay and the failure is reported in the footer --
          throwing away good data to display an error is not error handling.
        */}
        {!isLoadingFirstPage && errorMessage !== null && !hasIssues && (
          <IssueLoadError
            isRetrying={isRefreshing}
            message={errorMessage}
            onRetry={retry}
            title="Could not load issues"
          />
        )}

        {isEmpty && <IssueListEmpty onCreate={openComposer} />}

        {hasIssues && (
          <>
            {/*
              `role="list"` is not redundant. Safari drops list semantics from
              a list whose markers are removed, and `src/styles/base.css`
              removes them precisely for elements carrying this role -- so the
              role is both what restores the semantics and what opts into the
              reset.
            */}
            <ol className={styles.list} role="list">
              {issues.map((issue) => (
                <IssueRow issue={issue} key={issue.id} />
              ))}
            </ol>

            <div className={styles.listFooter}>
              {isLoadingMore ? (
                // Visually nothing like the first-load skeleton: a small
                // inline line beneath a list that is still entirely on screen.
                <span className={styles.loadingMore} role="status">
                  <span className={styles.spinner} aria-hidden="true" />
                  Loading more issues...
                </span>
              ) : loadMoreErrorMessage !== null ? (
                <span className={styles.inlineError} role="alert">
                  {loadMoreErrorMessage}
                  <Button onClick={loadMore} size="sm">
                    Try again
                  </Button>
                </span>
              ) : hasNextPage ? (
                <Button onClick={loadMore}>Load more</Button>
              ) : (
                // The end of the connection, stated rather than implied by a
                // button that would do nothing. "Loaded" and not "total": the
                // schema exposes no count, so this is a fact about this screen
                // and does not pretend to be a fact about the database.
                <span className={styles.endOfList}>
                  End of list &middot; {issues.length}{' '}
                  {issues.length === 1 ? 'issue' : 'issues'} loaded
                </span>
              )}
            </div>
          </>
        )}

        <p className={styles.footnote}>
          Priority names (Urgent, High, Medium, Low) are a convention of this
          interface. The API exposes priority only as an integer from 0 to 4
          and gives it no names.
        </p>
      </PageContent>
    </>
  )
}
