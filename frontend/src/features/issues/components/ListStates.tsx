import {
  Button,
  EmptyState,
  ErrorState,
  IssuesIcon,
  Skeleton,
  VisuallyHidden,
} from '../../../components'
import styles from '../issues.module.css'

/**
 * The three things the list can be instead of a list.
 *
 * They are deliberately three different shapes rather than one spinner with
 * three captions. A spinner tells the user only that something is happening;
 * these tell them, respectively, that rows are coming, that there are no rows
 * to come, and that asking failed and can be tried again -- which are three
 * unrelated situations with three different next actions.
 *
 * Two of the three are `components/States` with this feature's words in them.
 * Only the skeletons are local, because a skeleton has to be shaped like the
 * thing it stands in for and the shared primitive is one grey bar.
 */

const SKELETON_ROW_COUNT = 6

/**
 * First load, with nothing on screen yet.
 *
 * Shaped like the list it is about to become -- a stack of rows, each with a
 * glyph-sized block, a title-sized block and a meta-sized block in the same
 * places the real ones will be -- so the page does not jump when the data
 * arrives.
 *
 * Six rows and not `DEFAULT_PAGE_SIZE`: this is a hint about layout, not a
 * promise about how many rows are coming, and 25 shimmering bars is a worse
 * thing to look at than six.
 *
 * `role="status"` with hidden text, because `Skeleton` is `aria-hidden` by
 * design and a purely visual loading state is silence to a screen reader.
 */
export function IssueListSkeleton() {
  return (
    <div className={styles.skeletonList} role="status" aria-busy="true">
      <VisuallyHidden as="div">Loading issues</VisuallyHidden>
      {Array.from({ length: SKELETON_ROW_COUNT }, (_unused, index) => (
        <div className={styles.skeletonRow} key={index}>
          <Skeleton width="var(--glyph-column)" />
          <Skeleton className={styles.skeletonTitle} />
          <Skeleton width="var(--space-9)" />
        </div>
      ))}
    </div>
  )
}

/** The inspector's equivalent: a heading bar, a body block, a property block. */
export function IssueDetailSkeleton() {
  return (
    <div className={styles.skeletonDetail} role="status" aria-busy="true">
      <VisuallyHidden as="div">Loading issue</VisuallyHidden>
      <Skeleton height="var(--space-7)" width="12ch" />
      <Skeleton height="var(--space-7)" />
      <Skeleton height="var(--space-10)" />
      <Skeleton height="var(--space-10)" />
    </div>
  )
}

export interface IssueListEmptyProps {
  onCreate: () => void
}

/**
 * The server answered, and there are no issues.
 *
 * A success, not a failure, and `EmptyState` is styled as one -- no ARIA
 * role, because an empty list is not an alert and must not interrupt. The
 * button is here as well as in the shell's sidebar because this is the one
 * moment the sidebar's affordance is easiest to overlook: the middle of the
 * screen is empty and that is exactly where the user is looking.
 */
export function IssueListEmpty({ onCreate }: IssueListEmptyProps) {
  return (
    <EmptyState
      actions={
        <Button variant="primary" onClick={onCreate}>
          Create the first issue
        </Button>
      }
      description="Nothing has been created yet. The first issue you create will appear here."
      icon={<IssuesIcon />}
      title="No issues yet"
    />
  )
}

export interface IssueLoadErrorProps {
  title: string
  message: string
  onRetry: () => void
}

/**
 * The request failed.
 *
 * The message goes in `description` rather than in `detail`, because the
 * backend already keeps internals out of it -- expected pagination failures
 * are re-raised as a fixed string and everything else loses its cause on the
 * way out -- so it is safe to show, and hiding the only actionable sentence
 * behind a disclosure would leave the user with nothing.
 */
export function IssueLoadError({ title, message, onRetry }: IssueLoadErrorProps) {
  return <ErrorState description={message} onRetry={onRetry} title={title} />
}
