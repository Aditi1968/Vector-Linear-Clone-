import { useCallback, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'

import { PageContent, PageHeader } from '../../../app/layout'
import { PROJECT_ID_PARAM, useAppPaths } from '../../../app/routes'
import {
  Badge,
  Dialog,
  ErrorState,
  Menu,
  ProgressIndicator,
  Skeleton,
  VisuallyHidden,
} from '../../../components'
import type { MenuItem } from '../../../components'
import {
  useProjectActions,
  useProjectDetail,
  useProjectIssues,
  useProjectMembers,
  useProjectTeams,
  useUnfiledIssues,
} from '../api'
import type { MilestoneDraft, ProjectDraft, ProjectOutcome, ProjectValidationError } from '../api'
import { ProjectForm } from '../components/ProjectForm'
import { ProjectIssues } from '../components/ProjectIssues'
import { ProjectMilestones } from '../components/ProjectMilestones'
import { ProjectTeams } from '../components/ProjectTeams'
import {
  closedCount,
  formatDay,
  leadLabel,
  projectStateLabel,
  projectStateTone,
} from '../lib/projects'
import styles from '../projects.module.css'

const NO_ERRORS: readonly ProjectValidationError[] = []

/**
 * One project: what it is, who is on it, what it is broken into, and what is
 * being worked on.
 *
 * ## Route-backed
 *
 * The id comes from the URL, so reloading the page re-runs every query from
 * nothing and it renders the same way it did before the reload. There is no
 * hand-off of a project object from the list and no state that exists only if
 * the user arrived by clicking a row.
 *
 * ## Where the issue panel's numbers come from
 *
 * `filter: { projectId }`, so the issues below are the project's and every
 * number derived from them -- the header's progress, each milestone's -- is
 * about the project rather than about a page of the workspace. The one thing
 * still worth saying out loud is paging: while there is another page, the
 * progress labels say "among those loaded" and the panel says how many of the
 * total are on screen. See ../components/ProjectIssues.tsx.
 */
export function ProjectDetailPage() {
  const params = useParams()
  const projectId = params[PROJECT_ID_PARAM]
  const paths = useAppPaths()
  const navigate = useNavigate()

  const { project, isLoading, isNotFound, errorMessage, retry } = useProjectDetail(projectId)
  const { teams } = useProjectTeams()
  const { members } = useProjectMembers()
  const issueQuery = useProjectIssues(projectId)
  const unfiledIssues = useUnfiledIssues()

  // `?? ''` only ever reaches a hook whose functions are not called before
  // `project` exists -- every control that could call one is rendered inside
  // the `project !== null` branch below.
  const actions = useProjectActions(project?.id ?? '')

  const [isEditing, setIsEditing] = useState(false)
  const [formErrors, setFormErrors] = useState<readonly ProjectValidationError[]>(NO_ERRORS)
  const [formMessage, setFormMessage] = useState<string | null>(null)
  const [panelMessage, setPanelMessage] = useState<string | null>(null)

  /**
   * Report whatever a non-form write did wrong.
   *
   * Team and milestone writes have no form of their own to put a field error
   * beside, so both channels collapse into one line above the panels. The
   * validation errors are joined rather than dropped: they name the field
   * they are about, and the field is legible in the sentence even without a
   * control to attach it to.
   */
  const report = useCallback((outcome: ProjectOutcome<unknown>) => {
    if (outcome.status === 'ok') {
      setPanelMessage(null)
      return
    }

    setPanelMessage(
      outcome.status === 'failed'
        ? outcome.message
        : outcome.errors.map((error) => error.message).join(' '),
    )
  }, [])

  const openEditor = useCallback(() => {
    setFormErrors(NO_ERRORS)
    setFormMessage(null)
    setIsEditing(true)
  }, [])

  const closeEditor = useCallback(() => {
    setIsEditing(false)
  }, [])

  const handleUpdate = useCallback(
    (draft: ProjectDraft) => {
      void actions.updateProject(draft).then((outcome) => {
        if (outcome.status === 'ok') {
          setIsEditing(false)
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
    [actions],
  )

  const handleDelete = useCallback(() => {
    void actions.deleteProject().then((outcome) => {
      if (outcome.status === 'ok') {
        // Back to the list, because the page this was is gone. `replace` so
        // the browser's Back button does not return to a project that no
        // longer resolves.
        void navigate(paths.projects(), { replace: true })
        return
      }

      report(outcome)
    })
  }, [actions, navigate, paths, report])

  const handleMilestoneCreate = useCallback(
    (draft: MilestoneDraft) => {
      void actions.createMilestone(draft).then(report)
    },
    [actions, report],
  )

  const handleMilestoneUpdate = useCallback(
    (id: string, draft: MilestoneDraft) => {
      void actions.updateMilestone(id, draft).then(report)
    },
    [actions, report],
  )

  const handleMilestoneDelete = useCallback(
    (id: string) => {
      void actions.deleteMilestone(id).then(report)
    },
    [actions, report],
  )

  const handleTeamAdd = useCallback(
    (teamId: string) => {
      void actions.addTeam(teamId).then(report)
    },
    [actions, report],
  )

  const handleTeamRemove = useCallback(
    (teamId: string) => {
      void actions.removeTeam(teamId).then(report)
    },
    [actions, report],
  )

  const handlePlaceIssue = useCallback(
    (issueId: string, targetProjectId: string | null, milestoneId: string | null) => {
      void actions.placeIssue(issueId, targetProjectId, milestoneId).then(report)
    },
    [actions, report],
  )

  if (isLoading) {
    return (
      <>
        <PageHeader title="Project" />
        <PageContent>
          <div className={styles.skeletonStack} role="status" aria-busy="true">
            <VisuallyHidden as="div">Loading project</VisuallyHidden>
            <Skeleton width="14rem" height="1.5rem" />
            <Skeleton width="100%" height="8rem" />
          </div>
        </PageContent>
      </>
    )
  }

  if (errorMessage !== null) {
    return (
      <>
        <PageHeader title="Project" />
        <PageContent>
          <ErrorState
            title="Could not load this project"
            description={errorMessage}
            onRetry={retry}
          />
        </PageContent>
      </>
    )
  }

  if (isNotFound || project === null) {
    return (
      <>
        <PageHeader title="Project not found" />
        <PageContent>
          {/*
            One screen for "no such project" and for "a project in another
            workspace", because the server gives one answer to both -- null.
            Distinguishing them here would leak whether an id exists to
            someone who cannot see it.
          */}
          <ErrorState
            title="No such project"
            description="It may have been deleted, or it belongs to a workspace you cannot see."
            actions={<Link to={paths.projects()}>Back to projects</Link>}
          />
        </PageContent>
      </>
    )
  }

  const projectIssues = issueQuery.issues
  const closed = closedCount(projectIssues)
  const lead = leadLabel(project.leadId, members)

  const menuItems: readonly MenuItem[] = [
    { id: 'edit', label: 'Edit project', disabled: actions.isSaving, onSelect: openEditor },
    {
      id: 'delete',
      label: 'Delete project',
      destructive: true,
      disabled: actions.isSaving,
      separatorBefore: true,
      onSelect: handleDelete,
    },
  ]

  return (
    <>
      <PageHeader
        title={project.name}
        actions={
          <>
            <Badge tone={projectStateTone(project.state)}>
              {projectStateLabel(project.state)}
            </Badge>
            <Menu label={`Actions for ${project.name}`} items={menuItems} align="end" />
          </>
        }
      />

      <PageContent>
        {panelMessage !== null && (
          <p className={styles.formError} role="alert">
            {panelMessage}
          </p>
        )}

        <div className={styles.detail}>
          <div className={styles.column}>
            <section className={styles.panel} aria-labelledby="project-overview">
              <div className={styles.panelHeader}>
                <h2 className={styles.panelTitle} id="project-overview">
                  Overview
                </h2>
              </div>

              <div className={styles.panelBody}>
                {/* A real `<dl>`: the term/description association is what
                    makes a screen reader say "Target date, 14 March 2026"
                    rather than two unrelated fragments. */}
                <dl className={styles.facts}>
                  <dt className={styles.factTerm}>State</dt>
                  <dd className={styles.factValue}>{projectStateLabel(project.state)}</dd>

                  <dt className={styles.factTerm}>Target date</dt>
                  <dd className={styles.factValue}>
                    {project.targetDate === null ? (
                      <span className={styles.factEmpty}>Not set</span>
                    ) : (
                      <time dateTime={project.targetDate}>{formatDay(project.targetDate)}</time>
                    )}
                  </dd>

                  <dt className={styles.factTerm}>Lead</dt>
                  <dd className={styles.factValue}>
                    {lead.kind === 'named' ? (
                      lead.name
                    ) : (
                      <span className={styles.factEmpty}>
                        {lead.kind === 'none' ? 'No lead' : 'Someone you cannot see'}
                      </span>
                    )}
                  </dd>

                  <dt className={styles.factTerm}>Progress</dt>
                  <dd className={styles.factValue}>
                    <span className={styles.progressRow}>
                      <ProgressIndicator
                        value={closed}
                        total={projectIssues.length}
                        label={
                          issueQuery.hasNextPage
                            ? `${project.name}: closed issues among those loaded`
                            : `${project.name}: closed issues`
                        }
                        showLabel
                      />
                      <span>
                        {issueQuery.hasNextPage ? 'closed, of the issues loaded' : 'closed'}
                      </span>
                    </span>
                  </dd>
                </dl>

                {project.description !== null && project.description.trim() !== '' && (
                  <p className={styles.description}>{project.description}</p>
                )}
              </div>
            </section>

            <ProjectMilestones
              milestones={project.milestones}
              issues={projectIssues}
              isPartial={issueQuery.hasNextPage}
              isCounting={issueQuery.isLoading}
              isSaving={actions.isSaving}
              onCreate={handleMilestoneCreate}
              onUpdate={handleMilestoneUpdate}
              onDelete={handleMilestoneDelete}
            />
          </div>

          <div className={styles.column}>
            <section className={styles.panel} aria-labelledby="project-teams">
              <div className={styles.panelHeader}>
                <h2 className={styles.panelTitle} id="project-teams">
                  Teams
                </h2>
              </div>
              <div className={styles.panelBody}>
                <ProjectTeams
                  teamIds={project.teamIds}
                  teams={teams}
                  isSaving={actions.isSaving}
                  onAdd={handleTeamAdd}
                  onRemove={handleTeamRemove}
                />
              </div>
            </section>

            <ProjectIssues
              projectId={project.id}
              issues={projectIssues}
              totalCount={issueQuery.totalCount}
              unfiledIssues={unfiledIssues}
              milestones={project.milestones}
              isLoading={issueQuery.isLoading}
              isLoadingMore={issueQuery.isLoadingMore}
              hasNextPage={issueQuery.hasNextPage}
              errorMessage={issueQuery.errorMessage}
              isSaving={actions.isSaving}
              onLoadMore={issueQuery.loadMore}
              onPlace={handlePlaceIssue}
            />
          </div>
        </div>
      </PageContent>

      <Dialog open={isEditing} onClose={closeEditor} title="Edit project">
        <ProjectForm
          initial={{
            name: project.name,
            description: project.description,
            state: project.state,
            targetDate: project.targetDate,
            leadId: project.leadId,
          }}
          members={members}
          submitLabel="Save changes"
          isSubmitting={actions.isSaving}
          onCancel={closeEditor}
          onSubmit={handleUpdate}
          errors={formErrors}
          errorMessage={formMessage}
        />
      </Dialog>
    </>
  )
}
