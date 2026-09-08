import { Link } from 'react-router-dom'

import { useAppPaths } from '../../../app/routes'
import { Badge, Button, ErrorState, Skeleton, VisuallyHidden } from '../../../components'
import type { Environment } from '../../environments/api'
import { environmentKindLabel, environmentKindTone } from '../../environments/lib/environments'
import type { GithubIntegration } from '../../settings/api'
import type { Release, ReleaseDetail, ReleaseStatus } from '../api'
import {
  commitRange,
  formatInstant,
  nextStatuses,
  releaseStatusLabel,
  releaseStatusTone,
  transitionLabel,
} from '../lib/releases'
import styles from '../releases.module.css'

export interface ReleaseDetailPanelProps {
  /** The row that was selected. Drawn while the detail is still in flight. */
  summary: Release
  detail: ReleaseDetail | null
  environment: Environment | undefined
  repository: GithubIntegration['repositories'][number] | undefined
  isLoading: boolean
  isNotFound: boolean
  isSaving: boolean
  errorMessage: string | null
  onRetry: () => void
  onSetStatus: (status: ReleaseStatus) => void
  onDelete: () => void
}

/**
 * One release: what it shipped, where, and the note that went out with it.
 *
 * ## Every action lives here and none lives on a row
 *
 * A release name is not unique -- migration 024 refuses to make it so, because
 * the same version legitimately goes to staging and then to production, and
 * again after a rollback. A per-row menu would therefore give two rows the
 * same accessible name ("Actions on v1.4.0"), which is a screen you cannot
 * navigate by voice or by name lookup. Putting the actions on the one open
 * release makes each button unique on the page by construction rather than by
 * hoping the data differs.
 *
 * ## There is no Edit
 *
 * The schema has `releaseCreate`, `releaseStatusSet` and `releaseDelete` and
 * nothing else, and that is migration 024's central decision rather than a
 * missing endpoint: "the shape this file exists to make impossible is a
 * release note that changes after it was published." Said on screen, because
 * an interface that silently omits editing looks like one that forgot.
 *
 * ## The issues it shipped are ids
 *
 * `Release.issueIds` is a list of raw UUIDs; the schema exposes no
 * `Release.issues`, so nothing here can name them. They are rendered as links
 * to the issues themselves -- which do resolve -- rather than as a count
 * alone, and the panel says why the labels are ids.
 */
export function ReleaseDetailPanel({
  summary,
  detail,
  environment,
  repository,
  isLoading,
  isNotFound,
  isSaving,
  errorMessage,
  onRetry,
  onSetStatus,
  onDelete,
}: ReleaseDetailPanelProps) {
  const paths = useAppPaths()
  const moves = nextStatuses(summary.status)

  return (
    <section aria-labelledby="release-panel-title" className={styles.panel}>
      <div className={styles.panelHead}>
        <h2 className={styles.panelTitle} id="release-panel-title">
          {summary.name}
        </h2>
        <Badge tone={releaseStatusTone(summary.status)}>
          {releaseStatusLabel(summary.status)}
        </Badge>
      </div>

      <dl className={styles.facts}>
        <div className={styles.fact}>
          <dt>Environment</dt>
          <dd>
            {environment === undefined ? (
              /* The id is a real membership and the name is not resolvable:
                 `environments` is a plain list of everything this workspace
                 declared, so a miss means the target was removed or was never
                 the viewer's to see. Neither is "Staging". */
              <span className={styles.unresolved}>Not in this workspace’s list</span>
            ) : (
              <>
                {environment.name}{' '}
                <Badge tone={environmentKindTone(environment.kind)}>
                  {environmentKindLabel(environment.kind)}
                </Badge>
              </>
            )}
          </dd>
        </div>

        <div className={styles.fact}>
          <dt>Repository</dt>
          <dd>
            {repository === undefined ? (
              <span className={styles.unresolved}>
                Not covered by this workspace’s GitHub installation
              </span>
            ) : (
              repository.fullName
            )}
          </dd>
        </div>

        <div className={styles.fact}>
          <dt>Range</dt>
          {/* The abbreviations are what a person recognises; the full SHAs are
              in `title` because they are what the range actually resolves by
              and an abbreviation is ambiguous by construction. */}
          <dd className={styles.mono}>
            <span
              title={
                summary.previousCommitSha === null
                  ? summary.commitSha
                  : `${summary.previousCommitSha} to ${summary.commitSha}`
              }
            >
              {commitRange(summary)}
            </span>
          </dd>
        </div>

        <div className={styles.fact}>
          <dt>Cut</dt>
          <dd>
            <time dateTime={summary.createdAt}>{formatInstant(summary.createdAt)}</time>
          </dd>
        </div>

        <div className={styles.fact}>
          <dt>Deployed</dt>
          <dd>
            {summary.deployedAt === null ? (
              /* Not a dash. `releases_deployed_at_matches_status` makes the
                 status and the instant one fact, so a null here is the
                 database saying this release has never reached its target --
                 which is different from a value that failed to load. */
              <span className={styles.unresolved}>Never</span>
            ) : (
              <time dateTime={summary.deployedAt}>
                {formatInstant(summary.deployedAt)}
              </time>
            )}
          </dd>
        </div>
      </dl>

      <div className={styles.panelActions}>
        {moves.map((status) => (
          <Button
            disabled={isSaving}
            key={status}
            onClick={() => {
              onSetStatus(status)
            }}
            variant="secondary"
          >
            {transitionLabel(status)}
          </Button>
        ))}
        <Button disabled={isSaving} onClick={onDelete} variant="danger">
          Delete release
        </Button>
      </div>

      {moves.length === 0 && (
        <p className={styles.hint}>
          {releaseStatusLabel(summary.status)} is terminal. Retrying a deploy is a new
          release — a second attempt that reused this row would destroy this attempt’s
          timestamp, which is the one thing an incident review looks for.
        </p>
      )}

      <p className={styles.hint}>
        A release cannot be renamed or its notes edited. They were generated once, from
        the commits in the range above, and frozen — so the note somebody pasted into a
        changelog still says what it said.
      </p>

      {isLoading && (
        <div className={styles.skeletonStack} role="status" aria-busy="true">
          <VisuallyHidden as="div">Loading release notes</VisuallyHidden>
          <Skeleton width="100%" height="6rem" />
        </div>
      )}

      {!isLoading && errorMessage !== null && (
        <ErrorState
          title="Could not load this release"
          description={errorMessage}
          onRetry={onRetry}
        />
      )}

      {!isLoading && errorMessage === null && isNotFound && (
        <p className={styles.hint}>
          This release is no longer here. It may have been deleted, or it may never have
          been yours to see — the server gives one answer to both.
        </p>
      )}

      {detail !== null && (
        <>
          <h3 className={styles.sectionTitle}>Notes</h3>
          {/* `<pre>` and not a Markdown renderer. `app/domain/releases.py`
              generates this text; it is a document rather than markup, and
              parsing it here would mean a heading in a pull-request title
              rewriting the layout of the note. */}
          <pre className={styles.notes}>{detail.notes}</pre>

          <h3 className={styles.sectionTitle}>
            Issues shipped{' '}
            <span className={styles.count}>{detail.issueIds.length}</span>
          </h3>
          {detail.issueIds.length === 0 ? (
            <p className={styles.hint}>
              None. The range resolved to no commit mentioning an issue.
            </p>
          ) : (
            <>
              <ul className={styles.idList}>
                {detail.issueIds.map((issueId) => (
                  <li key={issueId}>
                    {/* The whole id, not a prefix. Eight characters of a UUID
                        look like an identifier and are not one; a reader who
                        wants to match this against something else needs all
                        of it. */}
                    <Link className={styles.idLink} to={paths.issue(issueId)}>
                      {issueId}
                    </Link>
                  </li>
                ))}
              </ul>
              <p className={styles.hint}>
                Listed by id: the API returns `issueIds` and exposes no issue objects on
                a release, so nothing here can show a key like ENG-142. Open one to see
                it.
              </p>
            </>
          )}

          <h3 className={styles.sectionTitle}>
            Pull requests{' '}
            <span className={styles.count}>{detail.pullRequestNumbers.length}</span>
          </h3>
          {detail.pullRequestNumbers.length === 0 ? (
            <p className={styles.hint}>None merged in this range.</p>
          ) : (
            <>
              <ul className={styles.numberList}>
                {detail.pullRequestNumbers.map((number) => (
                  <li className={styles.mono} key={number}>
                    #{number}
                  </li>
                ))}
              </ul>
              <p className={styles.hint}>
                Numbers only, and not links. The API publishes
                `pullRequestNumbers` and no URL, and a link built from a repository
                name here would be a guess at somebody else’s address.
              </p>
            </>
          )}
        </>
      )}
    </section>
  )
}
