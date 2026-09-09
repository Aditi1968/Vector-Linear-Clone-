import { useCallback, useState } from 'react'
import { Link } from 'react-router-dom'

import { PageContent, PageHeader } from '../../../app/layout'
import {
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
  Tag,
  VisuallyHidden,
  cx,
} from '../../../components'
import { useAppPaths } from '../../../app/routes'
import { useCreateProject, useProjectList, useProjectMembers, useProjectTeams } from '../api'
import type { ProjectDraft, ProjectValidationError } from '../api'
import { ProjectForm } from '../components/ProjectForm'
import {
  formatDay,
  isTargetLate,
  projectHealth,
  projectStateLabel,
  resolveTeams,
} from '../lib/projects'
import styles from '../projects.module.css'

/** The four health readings, as the classes that paint them. */
const HEALTH_CLASS = {
  'on-track': styles.onTrack,
  'at-risk': styles.atRisk,
  'off-track': styles.offTrack,
  planned: styles.planned,
} as const

const NO_ERRORS: readonly ProjectValidationError[] = []

/**
 * Every project in the workspace.
 *
 * `AppLayout` owns the `<main>` landmark and `PageHeader` owns the page's
 * only `<h1>`, so this screen renders neither -- a nested `main` produces two
 * "main" landmarks and defeats the landmark navigation the shell is built
 * around.
 *
 * The bottom of the list is a chain of four cases rather than a button with a
 * spinner, because they are not variations of one another: fetching the next
 * page, having failed to fetch it, having more to fetch, and having reached
 * the end. The last is the one usually skipped, leaving a "Load more" button
 * that does nothing -- so `hasNextPage: false` states the end of the list and
 * renders no button.
 */
export function ProjectListPage() {
  const paths = useAppPaths()

  const {
    projects,
    hasNextPage,
    isLoadingFirstPage,
    isLoadingMore,
    isRefreshing,
    errorMessage,
    loadMoreErrorMessage,
    loadMore,
    retry,
  } = useProjectList()

  // The teams are needed to name the ones a project is on. A failure here
  // costs team names and nothing else, so it is not raised to the page.
  const { teams } = useProjectTeams()
  const { members } = useProjectMembers()
  const { createProject, isSubmitting } = useCreateProject()

  const [isComposerOpen, setIsComposerOpen] = useState(false)
  const [formErrors, setFormErrors] = useState<readonly ProjectValidationError[]>(NO_ERRORS)
  const [formMessage, setFormMessage] = useState<string | null>(null)

  const openComposer = useCallback(() => {
    // Cleared on open rather than on close: a message about the *last*
    // attempt has no meaning against a blank form.
    setFormErrors(NO_ERRORS)
    setFormMessage(null)
    setIsComposerOpen(true)
  }, [])

  const closeComposer = useCallback(() => {
    setIsComposerOpen(false)
  }, [])

  const handleCreate = useCallback(
    (draft: ProjectDraft) => {
      void createProject(draft).then((outcome) => {
        if (outcome.status === 'ok') {
          // No confirmation banner. The dialog closes and the project is at
          // the head of the list behind it, which is both the confirmation
          // and the most direct evidence the refetch worked.
          setIsComposerOpen(false)
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
    [createProject],
  )

  const hasProjects = projects.length > 0
  const isEmpty = !isLoadingFirstPage && errorMessage === null && !hasProjects

  // One instant for the whole render, so two rows a tick apart cannot
  // disagree about whether the same date has gone by.
  const now = Date.now()

  return (
    <>
      <PageHeader
        title="Projects"
        // "Loaded" and not "active": the connection exposes no total and this
        // screen has no idea how many projects the workspace holds. A readout
        // that said "5 ACTIVE" would be reading the page, not the workspace.
        readout={hasProjects ? `${String(projects.length)} loaded` : undefined}
        actions={
          <Button variant="primary" onClick={openComposer}>
            New project
          </Button>
        }
      />

      <PageContent>
        {isLoadingFirstPage && (
          <div className={styles.skeletonStack} role="status" aria-busy="true">
            {/* `Skeleton` is `aria-hidden` by design -- a picture of text
                that does not exist yet. The announcement belongs on one live
                region for the whole list, which is this element. Six bars,
                not a page's worth: a hint about layout, not a promise about
                how many rows are coming. */}
            <VisuallyHidden as="div">Loading projects</VisuallyHidden>
            {Array.from({ length: 6 }, (_unused, index) => (
              <Skeleton key={index} width="100%" height="2.25rem" />
            ))}
          </div>
        )}

        {/* The whole-list error panel replaces the list only when there is no
            list to replace. If rows are loaded and a later request fails, the
            rows stay and the failure is reported in the footer -- throwing
            away good data to display an error is not error handling. */}
        {!isLoadingFirstPage && errorMessage !== null && !hasProjects && (
          <ErrorState
            title="Could not load projects"
            description={errorMessage}
            onRetry={isRefreshing ? undefined : retry}
          />
        )}

        {isEmpty && (
          <EmptyState
            icon={<ProjectIcon />}
            title="No projects yet"
            description="A project groups the issues behind one piece of work, across as many teams as it needs."
            actions={
              <Button variant="primary" onClick={openComposer}>
                Create the first project
              </Button>
            }
          />
        )}

        {hasProjects && (
          <>
            <List label="Projects">
              {projects.map((project) => {
                const memberships = resolveTeams(project.teamIds, teams)
                const health = projectHealth(project, now)

                return (
                  <ListRow interactive key={project.id}>
                    <ListRowMain>
                      {/* Name over state, which is the artboard's row: the
                          state is a legend on the project rather than a pill
                          floated to the far edge, where it had to compete
                          with the teams and the date for the eye. */}
                      <span className={styles.rowStack}>
                        <Link className={styles.rowLink} to={paths.project(project.id)}>
                          {project.name}
                        </Link>
                        <span className={cx(styles.rowState, HEALTH_CLASS[health])}>
                          {projectStateLabel(project.state)}
                        </span>
                      </span>
                    </ListRowMain>

                    <ListRowMeta>
                      {/* Teams on the row because a project spanning teams is
                          what a project *is* here, not a detail of one. */}
                      {memberships.length > 0 && (
                        <span className={styles.rowTeams}>
                          {memberships.map(({ teamId, team }) => (
                            <Tag key={teamId} name={team === null ? 'Team not visible' : team.key} />
                          ))}
                        </span>
                      )}

                      {project.targetDate !== null && (
                        <span
                          className={cx(
                            styles.rowDate,
                            isTargetLate(project, now) && styles.late,
                          )}
                        >
                          {/* The word matters: a bare date beside a project
                              could be a start, an end or a last edit. */}
                          Target <time dateTime={project.targetDate}>{formatDay(project.targetDate)}</time>
                        </span>
                      )}
                    </ListRowMeta>
                  </ListRow>
                )
              })}
            </List>

            <div className={styles.listFooter}>
              {isLoadingMore ? (
                <span role="status">Loading more projects...</span>
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
                // "Loaded" and not "total": the schema exposes no count, so
                // this is a fact about this screen and does not pretend to be
                // a fact about the database.
                <span className={styles.endOfList}>
                  End of list &middot; {projects.length}{' '}
                  {projects.length === 1 ? 'project' : 'projects'} loaded
                </span>
              )}
            </div>
          </>
        )}
      </PageContent>

      <Dialog
        open={isComposerOpen}
        onClose={closeComposer}
        title="New project"
        description="Projects are workspace-wide. Add the teams working on it from the project's own page."
      >
        <ProjectForm
          members={members}
          submitLabel="Create project"
          isSubmitting={isSubmitting}
          onCancel={closeComposer}
          onSubmit={handleCreate}
          errors={formErrors}
          errorMessage={formMessage}
        />
      </Dialog>
    </>
  )
}
