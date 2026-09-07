import { Button, List, Spinner } from '../components'
import { IssueRow } from './issues/components/IssueRow'
import type { IssueRowFields, WorkflowState, WorkspaceMember } from './issues/api'
import styles from './screens.module.css'

/**
 * The few things the seven workspace screens genuinely share.
 *
 * My Issues, Inbox, Team, Team issues, Members, Settings and Search each own
 * their own directory; this holds only what more than one of them needs, so
 * that "a list of issues" and "the bottom of a paginated list" look and
 * behave the same on every one of them rather than four times nearly.
 *
 * Nothing here fetches. These are presentation and two pure helpers.
 */

/**
 * Today as `YYYY-MM-DD` in the viewer's own timezone.
 *
 * For overdue comparisons against `Issue.dueDate`, which is a calendar day
 * and not an instant -- so the comparison has to be made in calendar days
 * too. `new Date().toISOString()` would give the UTC day and mark an issue
 * overdue several hours early west of Greenwich.
 */
export function localToday(): string {
  const now = new Date()

  return [
    now.getFullYear(),
    String(now.getMonth() + 1).padStart(2, '0'),
    String(now.getDate()).padStart(2, '0'),
  ].join('-')
}

/**
 * Turning a thrown value into something a person can read.
 *
 * Structural rather than `instanceof Error`, because Apollo Client v4 types
 * what an operation rejects with as `ErrorLike` -- an object with `name` and
 * `message` -- and not as `Error`.
 *
 * What arrives here is safe to show: the backend keeps internals out of its
 * public messages, re-raising expected failures as fixed strings and letting
 * everything else through GraphQL's normal error path `from None`.
 *
 * ponytail: a third copy of a nine-line function -- `features/issues/lib`
 * and `features/projects/api` each hold one already. Folding all three into
 * `src/lib` is the right fix and belongs to whoever owns that directory;
 * three copies that agree beat one import that crosses two feature
 * boundaries to reach a stranger's error helper.
 */
const FALLBACK_MESSAGE = 'Something went wrong. Please try again.'

export function describeError(reason: unknown): string {
  if (typeof reason === 'object' && reason !== null && 'message' in reason) {
    const { message } = reason

    if (typeof message === 'string' && message.trim().length > 0) {
      return message
    }
  }

  return FALLBACK_MESSAGE
}

export interface IssueRowsProps {
  /** The list's accessible name. Says which issues these are. */
  label: string
  issues: readonly IssueRowFields[]
  /** `Issue.workflowStateId` -> the state it names. */
  stateById: ReadonlyMap<string, WorkflowState>
  /** `Issue.assigneeId` -> the person it names. */
  memberById: ReadonlyMap<string, WorkspaceMember>
}

/**
 * A list of issues, drawn exactly as the issues screen draws one.
 *
 * `IssueRow` is imported from `features/issues` rather than reimplemented.
 * Two presentations of one row is how a product ends up with two different
 * ideas of what "overdue" looks like, and the row already resolves its own
 * link through `useAppPaths()`, carries the priority and status glyphs, and
 * is a real `<a>` so the keyboard reaches it.
 *
 * The lookups are props and not a query, because resolving
 * `workflowStateId` and `assigneeId` is one request for the whole screen and
 * a row that ran it would run it once per row.
 */
export function IssueRows({ label, issues, stateById, memberById }: IssueRowsProps) {
  const today = localToday()

  return (
    <List label={label}>
      {issues.map((issue) => (
        <IssueRow
          assignee={
            issue.assigneeId === null ? undefined : memberById.get(issue.assigneeId)
          }
          issue={issue}
          key={issue.id}
          selected={false}
          state={stateById.get(issue.workflowStateId)}
          today={today}
        />
      ))}
    </List>
  )
}

export interface ListFooterProps {
  /** What is being counted, singular: "issue", "notification". */
  noun: string
  /** How many rows are on screen. Never a total -- see below. */
  loadedCount: number
  hasNextPage: boolean
  isLoadingMore: boolean
  /** A failure of the most recent "load more", which leaves rows intact. */
  errorMessage: string | null
  onLoadMore: () => void
}

/**
 * The bottom of a cursor-paginated list.
 *
 * Four cases in a chain rather than a button with a spinner in it, because
 * they are not variations of one another: fetching the next page, having
 * failed to fetch it, having more to fetch, and having reached the end. The
 * last is the one usually skipped, which leaves a "Load more" button that
 * sends no request and changes nothing.
 *
 * "Loaded" and never "total": this states a fact about the screen. A caller
 * that has a `totalCount` to state says so above its list, where the number
 * can be put in a sentence rather than into a footer shared by six screens.
 */
export function ListFooter({
  noun,
  loadedCount,
  hasNextPage,
  isLoadingMore,
  errorMessage,
  onLoadMore,
}: ListFooterProps) {
  const plural = loadedCount === 1 ? noun : `${noun}s`

  return (
    <div className={styles.listFooter}>
      {isLoadingMore ? (
        // One announcement, not two: the live region carries the sentence and
        // the spinner beside it is decoration. A labelled `Spinner` would name
        // itself as well and the wait would be read out twice.
        <span role="status">
          <Spinner /> Loading more...
        </span>
      ) : errorMessage !== null ? (
        <span className={styles.inlineError} role="alert">
          {errorMessage}
          <Button onClick={onLoadMore} size="sm">
            Try again
          </Button>
        </span>
      ) : hasNextPage ? (
        <Button onClick={onLoadMore}>Load more</Button>
      ) : (
        <span>
          End of list &middot; {loadedCount} {plural} loaded
        </span>
      )}
    </div>
  )
}
