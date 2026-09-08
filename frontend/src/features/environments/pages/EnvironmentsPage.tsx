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
  SettingsIcon,
  Skeleton,
  VisuallyHidden,
} from '../../../components'
import { useReleaseList } from '../../releases/api'
import type { Release } from '../../releases/api'
import {
  formatInstant,
  releaseStatusLabel,
  releaseStatusTone,
} from '../../releases/lib/releases'
import { useEnvironmentActions, useEnvironmentList } from '../api'
import type { EnvironmentDraft, EnvironmentValidationError } from '../api'
import { EnvironmentForm } from '../components/EnvironmentForm'
import { environmentKindLabel, environmentKindTone } from '../lib/environments'
import styles from '../environments.module.css'

const NO_ERRORS: readonly EnvironmentValidationError[] = []

/**
 * Environments: where the product runs, and what is deployed to each.
 *
 * ## Two queries, and the second is borrowed
 *
 * `environments` answers the first half of that sentence. The second half --
 * "what is deployed to each" -- has no field of its own: the schema exposes no
 * `Environment.releases` and no `latestRelease`, so the only way to say it is
 * to read the release list and pick, per environment, the most recent release
 * that reached it.
 *
 * That read is `features/releases`' own hook, not a second copy of the query.
 * One document, one cache entry, one request, whichever screen asked first.
 *
 * ## And the answer is bounded, which is said out loud
 *
 * `releases` is a cursor connection and this screen takes ONE page of 25,
 * newest first. An environment whose last deploy is older than those 25 shows
 * nothing rather than showing the wrong release -- and the screen says the
 * answer is drawn from the recent releases rather than from all of them. A
 * "Load more" here would be a control whose only effect is on a footnote.
 *
 * ## What this screen deliberately cannot do
 *
 * Rename or remove a target. `environmentCreate` is the whole of the schema's
 * environment mutations. That is a real ceiling, not an omission this screen
 * papers over: `releases_environment_fk` is ON DELETE RESTRICT, so removing an
 * environment anything ever shipped to would have to decide what happens to
 * that history, and nothing has decided yet.
 */
export function EnvironmentsPage() {
  const { environments, isLoading, errorMessage, retry } = useEnvironmentList()

  const {
    releases,
    isLoadingFirstPage: isLoadingReleases,
    errorMessage: releasesErrorMessage,
  } = useReleaseList()

  const actions = useEnvironmentActions()

  const [isComposerOpen, setIsComposerOpen] = useState(false)
  const [formErrors, setFormErrors] = useState<readonly EnvironmentValidationError[]>(
    NO_ERRORS,
  )
  const [formMessage, setFormMessage] = useState<string | null>(null)

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
    (draft: EnvironmentDraft) => {
      void actions.createEnvironment(draft).then((outcome) => {
        if (outcome.status === 'ok') {
          closeComposer()
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

  const latest = latestDeployPerEnvironment(releases)

  return (
    <>
      <PageHeader
        title="Environments"
        description="Where the product runs, and what is deployed to each."
        actions={
          /* "New environment" and not "Add environment", which is what the
             composer's submit button says. Both are on the page at once while
             the dialog is open, and two controls with one accessible name is a
             screen you cannot navigate by voice or by name lookup. */
          <Button onClick={openComposer} variant="primary">
            New environment
          </Button>
        }
      />

      <PageContent>
        {isLoading && (
          <div className={styles.skeletonStack} role="status" aria-busy="true">
            <VisuallyHidden as="div">Loading environments</VisuallyHidden>
            {Array.from({ length: 3 }, (_unused, index) => (
              <Skeleton key={index} width="100%" height="2.75rem" />
            ))}
          </div>
        )}

        {!isLoading && errorMessage !== null && (
          <ErrorState
            title="Could not load environments"
            description={errorMessage}
            onRetry={retry}
          />
        )}

        {!isLoading && errorMessage === null && environments.length === 0 && (
          <EmptyState
            icon={<SettingsIcon />}
            title="No environments yet"
            description="An environment is a place a release goes — production, staging, a preview rig. A release has to name one, so nothing can ship until this list has an entry."
            actions={
              <Button onClick={openComposer} variant="primary">
                Declare the first environment
              </Button>
            }
          />
        )}

        {!isLoading && errorMessage === null && environments.length > 0 && (
          <div className={styles.stack}>
            <List label="Environments">
              {environments.map((environment) => {
                const deploy = latest.get(environment.id)

                return (
                  <ListRow key={environment.id}>
                    <ListRowMain>
                      <span className={styles.rowName}>{environment.name}</span>
                      <span className={styles.rowMeta}>
                        {isLoadingReleases ? (
                          <span>Looking for the last deploy…</span>
                        ) : deploy === undefined ? (
                          /* Not "never deployed". This is one page of
                             releases, so the honest statement is about what
                             was found, not about what exists. */
                          <span className={styles.unresolved}>
                            No deploy among the recent releases
                          </span>
                        ) : (
                          <>
                            <span>{deploy.name}</span>
                            <Badge tone={releaseStatusTone(deploy.status)}>
                              {releaseStatusLabel(deploy.status)}
                            </Badge>
                            {deploy.deployedAt !== null && (
                              <time dateTime={deploy.deployedAt}>
                                {formatInstant(deploy.deployedAt)}
                              </time>
                            )}
                          </>
                        )}
                      </span>
                    </ListRowMain>

                    <ListRowMeta>
                      <Badge tone={environmentKindTone(environment.kind)}>
                        {environmentKindLabel(environment.kind)}
                      </Badge>
                    </ListRowMeta>
                  </ListRow>
                )
              })}
            </List>

            <p className={styles.hint}>
              {releasesErrorMessage === null
                ? 'What is deployed to each target is read from the most recent releases, one page of them. A target whose last deploy is older than that shows nothing here rather than the wrong release.'
                : `The releases could not be loaded, so no target can say what is running on it: ${releasesErrorMessage}`}
            </p>

            <p className={styles.hint}>
              An environment cannot be renamed or removed. The API offers only creation,
              and anything ever deployed to a target holds it in place — deleting it would
              have to decide what happens to that shipping history.
            </p>
          </div>
        )}
      </PageContent>

      <Dialog
        open={isComposerOpen}
        onClose={closeComposer}
        title="Add environment"
        description="A name, and what kind of place it is."
      >
        <EnvironmentForm
          errorMessage={formMessage}
          errors={formErrors}
          isSaving={actions.isSaving}
          onCancel={closeComposer}
          onSubmit={handleCreate}
        />
      </Dialog>
    </>
  )
}

/**
 * The most recent release that actually reached each environment.
 *
 * `deployedAt` and not `createdAt`, because the question is what is RUNNING
 * there: a release cut on Monday and deployed on Friday is newer, on the only
 * axis that matters, than one cut on Wednesday and never deployed.
 * `releases_deployed_at_matches_status` makes that instant present exactly for
 * the two statuses that have reached the target, so a null is a reliable "this
 * one never got there" rather than a missing value.
 *
 * A `ROLLED_BACK` release still counts and is shown as such. It is the last
 * thing that reached the environment, and hiding it would make a target that
 * was rolled back look like one nothing ever went to.
 */
function latestDeployPerEnvironment(
  releases: readonly Release[],
): ReadonlyMap<string, Release> {
  const latest = new Map<string, Release>()

  for (const release of releases) {
    if (release.deployedAt === null) {
      continue
    }

    const current = latest.get(release.environmentId)

    // String comparison on ISO-8601, which sorts lexicographically iff the
    // strings are the same shape -- and they are, because they come from one
    // PostgreSQL `TIMESTAMPTZ` through one serialiser. Parsing to Date would
    // be the same comparison with a failure mode (`Invalid Date` compares
    // false against everything) this does not have.
    if (current === undefined || (current.deployedAt ?? '') < release.deployedAt) {
      latest.set(release.environmentId, release)
    }
  }

  return latest
}
