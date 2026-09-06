import { Button, cx, VisuallyHidden } from '../../../components'
import styles from '../issues.module.css'

/**
 * The three things the list can be instead of a list.
 *
 * They are deliberately three different shapes rather than one spinner with
 * three captions. A spinner tells the user only that something is happening;
 * these tell them, respectively, that rows are coming, that there are no rows
 * to come, and that asking failed and can be tried again -- which are three
 * unrelated situations with three different next actions.
 */

const SKELETON_ROW_COUNT = 6

/**
 * First load, with nothing on screen yet.
 *
 * Shaped like the list it is about to become -- a bordered stack of rows,
 * each with a badge-sized block, a title-sized block and a meta-sized block
 * in the same places the real ones will be -- so the page does not jump when
 * the data arrives.
 *
 * Six rows and not `DEFAULT_PAGE_SIZE`: this is a hint about layout, not a
 * promise about how many rows are coming, and 25 shimmering bars is a worse
 * thing to look at than six.
 *
 * `role="status"` with hidden text, because a purely visual loading state is
 * silence to a screen reader.
 */
export function IssueListSkeleton() {
  return (
    <div className={styles.skeletonList} role="status" aria-busy="true">
      <VisuallyHidden as="div">Loading issues</VisuallyHidden>
      {Array.from({ length: SKELETON_ROW_COUNT }, (_unused, index) => (
        <div className={styles.skeletonRow} key={index} aria-hidden="true">
          <span className={cx(styles.skeletonBar, styles.skeletonBadge)} />
          <span className={cx(styles.skeletonBar, styles.skeletonTitle)} />
          <span className={cx(styles.skeletonBar, styles.skeletonMeta)} />
        </div>
      ))}
    </div>
  )
}

/** The detail view's equivalent: a title bar, a facts panel, a body line. */
export function IssueDetailSkeleton() {
  return (
    <div role="status" aria-busy="true">
      <VisuallyHidden as="div">Loading issue</VisuallyHidden>
      <div aria-hidden="true">
        <div
          className={cx(styles.skeletonBar, styles.skeletonHeading, styles.skeletonBlock)}
        />
        <div
          className={cx(styles.skeletonBar, styles.skeletonPanel, styles.skeletonBlock)}
        />
        <div className={styles.skeletonBar} />
      </div>
    </div>
  )
}

export interface IssueListEmptyProps {
  onCreate: () => void
}

/**
 * The server answered, and there are no issues.
 *
 * A success, not a failure, and styled as one: a dashed panel rather than a
 * red one, and the single action that changes the situation. The button is
 * here as well as in the shell's sidebar because this is the one moment the
 * sidebar's affordance is easiest to overlook -- the middle of the screen is
 * empty and that is exactly where the user is looking.
 */
export function IssueListEmpty({ onCreate }: IssueListEmptyProps) {
  return (
    <div className={styles.empty}>
      <p className={styles.emptyTitle}>No issues yet</p>
      <p className={styles.emptyBody}>
        Nothing has been created yet. The first issue you create will appear
        here.
      </p>
      <Button variant="primary" onClick={onCreate}>
        Create the first issue
      </Button>
    </div>
  )
}

export interface IssueLoadErrorProps {
  title: string
  message: string
  onRetry: () => void
  isRetrying?: boolean
}

/**
 * The request failed.
 *
 * The message is the one the server or the browser produced. The backend
 * already keeps internals out of it -- expected pagination failures are
 * re-raised as a fixed string and everything else loses its cause on the way
 * out -- so it is shown rather than replaced with a generic apology that
 * would leave the user with nothing to act on.
 */
export function IssueLoadError({
  title,
  message,
  onRetry,
  isRetrying = false,
}: IssueLoadErrorProps) {
  return (
    <div className={styles.error} role="alert">
      <p className={styles.errorTitle}>{title}</p>
      <p className={styles.errorBody}>{message}</p>
      <Button onClick={onRetry} disabled={isRetrying}>
        {isRetrying ? 'Retrying...' : 'Try again'}
      </Button>
    </div>
  )
}
