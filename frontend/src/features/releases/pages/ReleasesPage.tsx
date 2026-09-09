import { useCallback, useState } from 'react'

import { PageContent, PageHeader } from '../../../app/layout'
import {
  Badge,
  Button,
  Dialog,
  EmptyState,
  ErrorState,
  List,
  ListRow,
  ListRowMain,
  ListRowMeta,
  ProjectIcon,
  Skeleton,
  VisuallyHidden,
} from '../../../components'
import { useEnvironmentList } from '../../environments/api'
import { environmentsById } from '../../environments/lib/environments'
import { useIntegrations } from '../../settings/api'
import type { GithubIntegration } from '../../settings/api'
import { useReleaseActions, useReleaseDetail, useReleaseList } from '../api'
import type { Release, ReleaseDraft, ReleaseStatus, ReleaseValidationError } from '../api'
import { ReleaseDetailPanel } from '../components/ReleaseDetailPanel'
import { ReleaseForm } from '../components/ReleaseForm'
import {
  commitRange,
  formatInstant,
  releaseStatusLabel,
  releaseStatusTone,
} from '../lib/releases'
import styles from '../releases.module.css'

const NO_ERRORS: readonly ReleaseValidationError[] = []

/** Stable identity for "GitHub is not connected", so the form is not remounted. */
const NO_REPOSITORIES: GithubIntegration['repositories'] = []

/**
 * Releases: what shipped, where it went, and the note that went out with it.
 *
 * ## Why this is one screen and not a list plus a detail route
 *
 * `ROUTE_SEGMENTS` declares `releases` and no `releases/:id`, so there is no
 * per-release URL to navigate to. Selecting one therefore changes what the
 * panel beside the list shows rather than pushing history, and the rows are
 * `<button>`s rather than links -- an `<a>` with nowhere to point breaks
 * middle-click, "open in new tab" and the back button all at once.
 *
 * ## Three queries, and two of them belong to other features
 *
 * A release carries an `environmentId` and a `repositoryId` and the schema
 * exposes no object for either, so naming them means resolving the ids
 * against lists that live elsewhere: `environments` (features/environments)
 * and `githubIntegration.repositories` (features/settings). Both are read
 * through those features' own hooks rather than through a second copy of the
 * query here -- one document, one cache entry, one request, whichever screen
 * asked first.
 *
 * A failure of either costs those two columns and nothing else: the release
 * list still renders, and the panel says the id did not resolve rather than
 * inventing a name for it.
 *
 * ## What this screen deliberately cannot do
 *
 * Edit a release. `releaseCreate`, `releaseStatusSet` and `releaseDelete` are
 * the whole of the schema's release mutations, because migration 024 exists
 * to make a release note that changes after publication impossible. The panel
 * says so.
 */
export function ReleasesPage() {
  const {
    releases,
    hasNextPage,
    isLoadingFirstPage,
    isLoadingMore,
    errorMessage,
    loadMoreErrorMessage,
    loadMore,
    retry,
  } = useReleaseList()

  const { environments, isLoading: isLoadingEnvironments } = useEnvironmentList()
  const { github, isLoading: isLoadingIntegrations } = useIntegrations()

  const actions = useReleaseActions()

  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [isComposerOpen, setIsComposerOpen] = useState(false)
  const [formErrors, setFormErrors] = useState<readonly ReleaseValidationError[]>(
    NO_ERRORS,
  )
  const [formMessage, setFormMessage] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)

  // The first release until somebody chooses otherwise, so the panel has
  // something to show rather than an empty frame beside a full list.
  const selected =
    releases.find((release) => release.id === selectedId) ?? releases[0] ?? null

  const detail = useReleaseDetail(selected?.id ?? null)

  const byEnvironment = environmentsById(environments)
  const repositories = github?.repositories ?? NO_REPOSITORIES

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

  const handleCreate = useCallback(
    (draft: ReleaseDraft) => {
      void actions.createRelease(draft).then((outcome) => {
        if (outcome.status === 'ok') {
          closeComposer()
          // Select the release just cut, so the notes it generated are what
          // the panel shows next. That is the whole reason somebody pressed
          // the button.
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

  /**
   * Report a write that was not made through a form.
   *
   * Above the list rather than beside the control: the row it refers to may
   * have moved by the time it is read, and a refusal anchored to a vanished
   * row is one nobody sees.
   */
  const handleSetStatus = useCallback(
    (status: ReleaseStatus) => {
      if (selected === null) {
        return
      }

      void actions.setStatus(selected.id, status).then((outcome) => {
        if (outcome.status === 'ok') {
          setActionError(null)
          return
        }

        setActionError(
          outcome.status === 'failed'
            ? outcome.message
            : (outcome.errors[0]?.message ??
              'That release could not be moved to that status.'),
        )
      })
    },
    [actions, selected],
  )

  const handleDelete = useCallback(() => {
    if (selected === null) {
      return
    }

    void actions.deleteRelease(selected.id).then((outcome) => {
      if (outcome.status === 'ok') {
        setActionError(null)

        // Selecting nothing rather than the next row: which release follows a
        // deleted one is the refetched list's answer, and `selected` falls
        // back to the first on its own.
        setSelectedId(null)
        return
      }

      setActionError(
        outcome.status === 'failed'
          ? outcome.message
          : (outcome.errors[0]?.message ?? 'That release could not be deleted.'),
      )
    })
  }, [actions, selected])

  const isBusy = isLoadingFirstPage

  return (
    <>
      <PageHeader
        title="Releases"
        description="What shipped, and what is going out next."
        actions={
          <Button onClick={openComposer} variant="primary">
            Cut a release
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
            <VisuallyHidden as="div">Loading releases</VisuallyHidden>
            {Array.from({ length: 4 }, (_unused, index) => (
              <Skeleton key={index} width="100%" height="2.75rem" />
            ))}
          </div>
        )}

        {!isBusy && errorMessage !== null && (
          <ErrorState
            title="Could not load releases"
            description={errorMessage}
            onRetry={retry}
          />
        )}

        {!isBusy && errorMessage === null && releases.length === 0 && (
          <EmptyState
            icon={<ProjectIcon />}
            title="Nothing has shipped yet"
            description="A release is a reading of what changed between two commits, frozen at the moment it was cut — the note it generates is what goes into a changelog and does not change afterwards."
            actions={
              <Button onClick={openComposer} variant="primary">
                Cut the first release
              </Button>
            }
          />
        )}

        {!isBusy && errorMessage === null && releases.length > 0 && (
          <div className={styles.split}>
            <div className={styles.column}>
              <List label="Releases">
                {releases.map((release) => (
                  <ListRow
                    interactive
                    key={release.id}
                    selected={release.id === selected?.id}
                  >
                    <ListRowMain>
                      <button
                        aria-current={release.id === selected?.id}
                        className={styles.rowButton}
                        onClick={() => {
                          setSelectedId(release.id)
                        }}
                        type="button"
                      >
                        <span className={styles.rowName}>{release.name}</span>
                        <span className={styles.rowMeta}>
                          <span>{environmentName(byEnvironment, release)}</span>
                          {/* `.rowMeta` is already mono; the SHA range and the
                              instant need no class of their own. */}
                          <span>{commitRange(release)}</span>
                          {/* The instant, not the day: the same version ships
                              twice in one day often enough that two rows
                              reading "7 Sep 2026" would be two different
                              deploys wearing one label. */}
                          <time dateTime={release.createdAt}>
                            {formatInstant(release.createdAt)}
                          </time>
                        </span>
                      </button>
                    </ListRowMain>

                    <ListRowMeta>
                      <Badge tone={releaseStatusTone(release.status)}>
                        {releaseStatusLabel(release.status)}
                      </Badge>
                    </ListRowMeta>
                  </ListRow>
                ))}
              </List>

              {loadMoreErrorMessage !== null && (
                <p className={styles.actionError} role="alert">
                  {loadMoreErrorMessage}
                </p>
              )}

              {hasNextPage && (
                <div className={styles.loadMore}>
                  {/* Said plainly: there is no count field on this connection,
                      so "of N" is a number nobody can supply. */}
                  <p className={styles.countLine}>
                    Showing <span className={styles.count}>{releases.length}</span>{' '}
                    releases, newest first. There are more.
                  </p>
                  <Button disabled={isLoadingMore} onClick={loadMore} variant="secondary">
                    {isLoadingMore ? 'Loading…' : 'Load more'}
                  </Button>
                </div>
              )}
            </div>

            {selected !== null && (
              <div className={styles.column}>
                <ReleaseDetailPanel
                  detail={detail.release}
                  environment={byEnvironment.get(selected.environmentId)}
                  errorMessage={detail.errorMessage}
                  isLoading={detail.isLoading}
                  isNotFound={detail.isNotFound}
                  isSaving={actions.isSaving}
                  onDelete={handleDelete}
                  onRetry={detail.retry}
                  onSetStatus={handleSetStatus}
                  repository={repositories.find(
                    (candidate) => candidate.repositoryId === selected.repositoryId,
                  )}
                  summary={selected}
                />
              </div>
            )}
          </div>
        )}
      </PageContent>

      <Dialog
        open={isComposerOpen}
        onClose={closeComposer}
        title="Cut a release"
        description="The version, where it goes, and the commit it ships."
      >
        {isLoadingEnvironments || isLoadingIntegrations ? (
          <div className={styles.skeletonStack} role="status" aria-busy="true">
            <VisuallyHidden as="div">Loading environments and repositories</VisuallyHidden>
            <Skeleton width="100%" height="8rem" />
          </div>
        ) : (
          <ReleaseForm
            environments={environments}
            errorMessage={formMessage}
            errors={formErrors}
            isSaving={actions.isSaving}
            onCancel={closeComposer}
            onSubmit={handleCreate}
            repositories={repositories}
          />
        )}
      </Dialog>
    </>
  )
}

/**
 * What a row calls the environment a release went to.
 *
 * A miss is stated rather than blanked. `environments` returns everything the
 * workspace declared, so an id with no match means the target was removed or
 * was never the viewer's to see -- and a row that simply omitted the column
 * would read as a release that went nowhere.
 */
function environmentName(
  byId: ReadonlyMap<string, { name: string }>,
  release: Release,
): string {
  return byId.get(release.environmentId)?.name ?? 'Unknown environment'
}
