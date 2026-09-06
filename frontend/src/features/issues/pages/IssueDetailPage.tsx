import { Link, useParams } from 'react-router-dom'

import { PageContent, PageHeader } from '../../../app/layout'
import { cx } from '../../../components'
import { ISSUE_ID_PARAM, useAppPaths } from '../../../app/routes'
import { useIssueDetail } from '../api'
import { PriorityBadge } from '../components/PriorityBadge'
import { IssueDetailSkeleton, IssueLoadError } from '../components/ListStates'
import styles from '../issues.module.css'
import { formatAbsolute, formatRelative } from '../lib/dates'

/**
 * The page title for each state.
 *
 * `PageHeader` renders the screen's only `<h1>`, so it needs a title before
 * the issue has arrived. Naming the states explicitly beats a chain of
 * ternaries in the JSX, and it keeps "Issue not found" from being announced
 * as a heading while the request is still in flight.
 */
function headingFor(
  isLoading: boolean,
  isNotFound: boolean,
  title: string | undefined,
): string {
  if (title !== undefined) {
    return title
  }

  if (isLoading) {
    return 'Issue'
  }

  return isNotFound ? 'Issue not found' : 'Issue'
}

/**
 * One issue.
 *
 * ## Route-backed, which is what makes a refresh work
 *
 * The id is read from the URL and the query runs from it. No issue object is
 * handed over from the list, and there is no state that only exists if the
 * user got here by clicking a row -- so loading `/issues/<id>` directly, or
 * pressing F5 on it, renders exactly the same page by exactly the same path.
 * (Serving that URL is the dev server's and the deployment's job: a browser
 * router needs the server to return index.html for unknown paths. Vite's dev
 * server does this by default.)
 *
 * ## Not found is a state, not an error
 *
 * `issue(id:)` is nullable in the schema, so null is a successful answer
 * meaning the row is not there. It gets its own screen with a way back to the
 * list, rather than the red panel a transport failure gets, because the two
 * need different things from the user.
 *
 * ## Only what the server has
 *
 * Seven fields, all of them rendered from the response. There is no activity
 * feed, no comment thread, no status timeline and no "created by": the schema
 * has none of them, and a section header with nothing real under it is worse
 * than an absent section.
 */
export function IssueDetailPage() {
  const issueId = useParams()[ISSUE_ID_PARAM]
  const paths = useAppPaths()
  const { issue, isLoading, isNotFound, errorMessage, retry } = useIssueDetail(issueId)

  return (
    <>
      <PageHeader title={headingFor(isLoading, isNotFound, issue?.title)} />

      <PageContent constrained>
        <Link className={styles.backLink} to={paths.issues()}>
          &larr; All issues
        </Link>

        {isLoading && <IssueDetailSkeleton />}

        {!isLoading && errorMessage !== null && (
          <IssueLoadError
            message={errorMessage}
            onRetry={retry}
            title="Could not load this issue"
          />
        )}

        {!isLoading && errorMessage === null && isNotFound && (
          // No heading inside the panel: `PageHeader` has already put "Issue
          // not found" in the page's `<h1>`, and repeating it two lines below
          // says the same thing twice to a sighted reader and announces a
          // phantom second heading to a screen reader.
          <div className={styles.empty}>
            <p className={styles.emptyBody}>
              No issue exists for this id. The link may be wrong or incomplete.
            </p>
            <Link className={styles.backLink} to={paths.issues()}>
              Back to issues
            </Link>
          </div>
        )}

        {issue !== null && (
          <article>
            <div className={styles.detailFacts}>
              <span className={styles.detailFactLabel}>Priority</span>
              <p className={styles.detailFactValue}>
                <PriorityBadge value={issue.priority} />
              </p>

              <span className={styles.detailFactLabel}>Created</span>
              <p className={styles.detailFactValue}>
                <time dateTime={issue.createdAt}>{formatAbsolute(issue.createdAt)}</time>{' '}
                <span className={styles.fieldHint}>
                  ({formatRelative(issue.createdAt)})
                </span>
              </p>

              <span className={styles.detailFactLabel}>Updated</span>
              <p className={styles.detailFactValue}>
                <time dateTime={issue.updatedAt}>{formatAbsolute(issue.updatedAt)}</time>
              </p>

              {/* Only when the server has one; see IssueRow for why. */}
              {issue.completedAt !== null && (
                <>
                  <span className={styles.detailFactLabel}>Completed</span>
                  <p className={styles.detailFactValue}>
                    <time dateTime={issue.completedAt}>
                      {formatAbsolute(issue.completedAt)}
                    </time>
                  </p>
                </>
              )}

              {/*
                The UUID is shown because it is the only identifier this
                product has. There is no short human-readable key like VEC-42
                anywhere in the schema, and inventing one on the client would
                make a label that no API call, no search and no colleague's
                link would accept.
              */}
              <span className={styles.detailFactLabel}>ID</span>
              <p className={cx(styles.detailFactValue, styles.detailId)}>{issue.id}</p>
            </div>

            <h2 className={styles.detailSectionHeading}>Description</h2>
            {issue.description === null || issue.description.trim().length === 0 ? (
              <p className={styles.descriptionEmpty}>No description.</p>
            ) : (
              // `white-space: pre-wrap` in the stylesheet, so line breaks the
              // author typed survive. Rendered as text and never as HTML or
              // Markdown: the server stores a plain string and says nothing
              // about it being either.
              <p className={styles.description}>{issue.description}</p>
            )}
          </article>
        )}
      </PageContent>
    </>
  )
}
