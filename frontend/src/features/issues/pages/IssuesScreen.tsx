import { useCallback, useEffect, useState } from 'react'
import type { KeyboardEvent as ReactKeyboardEvent } from 'react'
import { useNavigate, useParams } from 'react-router-dom'

import {
  PageContent,
  PageHeader,
  ViewControls,
  useGroupMode,
  useRegisterCreateIssueAction,
} from '../../../app/layout'
import { ISSUE_ID_PARAM, useAppPaths } from '../../../app/routes'
import { useWorkspace } from '../../../app/workspace'
import {
  Button,
  GroupHeader,
  Kbd,
  List,
  Spinner,
  StatusIndicator,
} from '../../../components'
import { useIssueList, useWorkspaceContext } from '../api'
import type { IssueRowFields } from '../api'
import { IssueComposer } from '../components/IssueComposer'
import { IssueInspector } from '../components/IssueInspector'
import { IssueRow } from '../components/IssueRow'
import {
  IssueListEmpty,
  IssueListSkeleton,
  IssueLoadError,
} from '../components/ListStates'
import styles from '../issues.module.css'
import { groupIssuesByState } from '../lib/grouping'
import { isTypingTarget } from '../lib/keyboard'

/** Today as `YYYY-MM-DD` in the viewer's own timezone, for overdue comparisons. */
function localToday(): string {
  const now = new Date()

  return [
    now.getFullYear(),
    String(now.getMonth() + 1).padStart(2, '0'),
    String(now.getDate()).padStart(2, '0'),
  ].join('-')
}

/**
 * The issues screen: the list, and whichever issue the URL has open beside it.
 *
 * ## One component behind two routes
 *
 * `/:workspaceSlug/issues` and `/:workspaceSlug/issues/:issueId` both render
 * this. That is what makes the split pane work at all: the list is not
 * unmounted when an issue opens, so its scroll position, its loaded pages and
 * its keyboard focus all survive. Exporting the same function under both
 * names (see ../index.ts) also means React reconciles the two routes as the
 * same component rather than tearing the screen down and rebuilding it.
 *
 * ## The URL is the selection
 *
 * There is no `selectedIssue` state anywhere in this file. Which issue is
 * open is read from the route param, every row links to a URL rather than
 * calling a handler, and closing the inspector is a navigation. So a refresh,
 * a pasted link, the back button and an opened-in-new-tab row all land on the
 * same screen -- and the failure this prevents (a detail pane that is blank
 * after F5 because the object was handed over in memory) cannot be written.
 *
 * ## Shell integration
 *
 * `AppLayout` owns the `<main>` landmark and `PageHeader` owns the page's
 * only `<h1>`, so this screen renders neither: a nested `main` produces two
 * "main" landmarks and defeats the landmark navigation the shell is built
 * around. The inspector's heading is an `<h2>`, which is also why the `<h1>`
 * stays "Issues" while an issue is open -- the list is still the page.
 *
 * ## Pagination
 *
 * Cursor-based, and there is no number anywhere in this file that could be an
 * offset. "Load more" sends `pageInfo.endCursor` as `after`; the cache's field
 * policy merges the page onto the ones already loaded. Rows on screen stay on
 * screen while the next page is in flight, because nothing here clears them
 * and the merge policy only ever adds.
 *
 * ## Keyboard
 *
 * One window listener for the screen-level keys, and one React handler on the
 * list for arrow navigation. That is the whole keyboard implementation: no
 * registry, no provider, no key-sequence machinery. Rows are links, so Enter
 * and middle-click already work and are not handled here.
 *
 * ## Flat or grouped
 *
 * The header's two view controls are the design's, and this screen is the one
 * that honours both. Density is free -- it is an attribute on the document and
 * every row measures itself against `--row-height` -- but grouping changes
 * what is rendered, so it is read here and applied below. See
 * ../lib/grouping.ts for why a workspace-wide list groups by state *name*.
 */
export function IssuesScreen() {
  const openIssueId = useParams()[ISSUE_ID_PARAM]
  const navigate = useNavigate()
  const paths = useAppPaths()
  const { workspace } = useWorkspace()
  const [groupMode] = useGroupMode()

  const {
    issues,
    hasNextPage,
    isLoadingFirstPage,
    isLoadingMore,
    errorMessage,
    loadMoreErrorMessage,
    loadMore,
    retry,
  } = useIssueList()

  // One lookup for the whole screen. Rows receive what they need as props;
  // 25 rows each resolving their own assignee would be 25 subscriptions to
  // the same query.
  const { stateById, memberById } = useWorkspaceContext()

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
        // has to handle. The composer is closed first when both are open,
        // because it is the thing in front.
        if (isComposerOpen) {
          event.preventDefault()
          closeComposer()
          return
        }

        if (openIssueId !== undefined) {
          event.preventDefault()
          void navigate(paths.issues())
        }

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
  }, [closeComposer, isComposerOpen, navigate, openComposer, openIssueId, paths])

  /**
   * Up and down move between rows.
   *
   * Focus is moved rather than a selection index kept in state, so the
   * browser's own focus is the cursor: the row that is focused is the row
   * Enter will open, and there is no second notion of "current row" to fall
   * out of step with it. Rows are found in the DOM in render order for the
   * same reason -- a parallel ref array would be a copy of something the DOM
   * already knows.
   */
  const handleListKeys = useCallback((event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') {
      return
    }

    const rows = [
      ...event.currentTarget.querySelectorAll<HTMLAnchorElement>('[data-issue-row]'),
    ]
    const current = rows.findIndex((row) => row === document.activeElement)

    // Nothing in the list has focus, so this keystroke is somebody else's --
    // most likely a page scroll, which must keep working.
    if (current === -1) {
      return
    }

    const next = rows[current + (event.key === 'ArrowDown' ? 1 : -1)]

    // No wrapping. At either end the keystroke falls through to the browser,
    // which scrolls -- which is what a user pressing down at the bottom of a
    // list is asking for.
    if (next === undefined) {
      return
    }

    event.preventDefault()
    next.focus()
  }, [])

  const hasIssues = issues.length > 0
  const isEmpty = !isLoadingFirstPage && errorMessage === null && !hasIssues
  const today = localToday()

  // Null whenever the list is flat, and also whenever it *cannot* honestly be
  // grouped -- a state the workspace context has not resolved yet. Either way
  // the flat list is what renders, so the grouped view never appears half
  // built.
  const groups =
    groupMode === 'grouped' ? groupIssuesByState(issues, stateById) : null

  const renderRow = (issue: IssueRowFields) => (
    <IssueRow
      assignee={
        issue.assigneeId === null ? undefined : memberById.get(issue.assigneeId)
      }
      issue={issue}
      key={issue.id}
      selected={issue.id === openIssueId}
      state={stateById.get(issue.workflowStateId)}
      today={today}
    />
  )

  return (
    <>
      <PageHeader
        title="Issues"
        // The workspace's real name, from the shell's own membership record.
        // The design's subtitle names the workspace, and a hardcoded one here
        // would be a tenant identifier invented by the frontend.
        description={`Every issue in ${workspace.name}, newest first.`}
        actions={
          <>
            <ViewControls grouping />
            <span className={styles.fieldHint}>
              Press <Kbd>C</Kbd> to create
            </span>
          </>
        }
      />

      <PageContent className={styles.workspace}>
        <div className={styles.listPane}>
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
              message={errorMessage}
              onRetry={retry}
              title="Could not load issues"
            />
          )}

          {isEmpty && <IssueListEmpty onCreate={openComposer} />}

          {hasIssues && (
            <div onKeyDown={handleListKeys}>
              {groups === null ? (
                <List label="Issues">{issues.map(renderRow)}</List>
              ) : (
                /*
                  A header and then a `List` carrying the same name, which is
                  the contract GroupHeader documents: a screen reader hears
                  "In progress, list, 6 items" and can skip the run. The
                  header's glyph is decorative -- GroupHeader hides it -- since
                  the name beside it already says what the group is.

                  Up and down still cross group boundaries, because the key
                  handler above collects every `[data-issue-row]` under this
                  one wrapper rather than per list.
                */
                groups.map((group) => (
                  <section className={styles.group} key={group.key}>
                    <GroupHeader
                      count={group.issues.length}
                      icon={
                        <StatusIndicator
                          category={group.category}
                          name={group.name}
                        />
                      }
                      name={group.name}
                    />
                    <List label={group.name}>{group.issues.map(renderRow)}</List>
                  </section>
                ))
              )}

              <div className={styles.listFooter}>
                {isLoadingMore ? (
                  // Visually nothing like the first-load skeleton: a small
                  // inline line beneath a list that is still entirely on
                  // screen.
                  // One announcement, not two: the live region carries the
                  // sentence and the spinner beside it is decoration. A
                  // labelled Spinner here would name itself as well, and a
                  // screen reader would hear the wait twice.
                  <span className={styles.loadingMore} role="status">
                    <Spinner />
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
                  // The end of the connection, stated rather than implied by
                  // a button that would do nothing. "Loaded" and not "total":
                  // the schema exposes no count, so this is a fact about this
                  // screen and does not pretend to be a fact about the
                  // database.
                  <span className={styles.endOfList}>
                    End of list &middot; {issues.length}{' '}
                    {issues.length === 1 ? 'issue' : 'issues'} loaded
                  </span>
                )}
              </div>
            </div>
          )}
        </div>

        {openIssueId !== undefined && (
          /*
            `key` on the id, so moving between issues gets a fresh panel
            rather than one still holding the previous issue's validation
            messages. A complementary landmark, so a screen reader can jump
            straight to it and back out again.
          */
          <aside aria-label="Issue detail" className={styles.inspectorPane}>
            <IssueInspector issueId={openIssueId} key={openIssueId} />
          </aside>
        )}
      </PageContent>
    </>
  )
}
