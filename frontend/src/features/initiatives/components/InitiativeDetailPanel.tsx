import { useId, useState } from 'react'
import { Link } from 'react-router-dom'

import { useAppPaths } from '../../../app/routes'
import {
  Badge,
  Button,
  EmptyState,
  ErrorState,
  InspectorPanel,
  Select,
  Skeleton,
  Textarea,
  VisuallyHidden,
} from '../../../components'
import { memberLabel } from '../../issues/api'
import type { WorkspaceMember, WorkspaceProject } from '../../issues/api'
import { formatRelative } from '../../issues/lib/dates'
import { formatDay, projectStateLabel } from '../../projects/lib/projects'
import type { Health, Initiative, InitiativeDetail } from '../api'
import {
  HEALTHS,
  HEALTH_UNREPORTED,
  addableProjects,
  healthLabel,
  healthTone,
  initiativeStatusLabel,
  initiativeStatusTone,
  parentCandidates,
  resolveProjects,
} from '../lib/initiatives'
import styles from '../initiatives.module.css'

/**
 * How many projects `IssueWorkspaceContext` asks for.
 *
 * Copied from `features/issues/api/operations.graphql`, which selects
 * `projects(workspaceSlug:, first: 50)` and no `pageInfo` -- so there is no
 * way from here to ask whether there are more. A full page is the only signal
 * available, and it is ambiguous by exactly one project: at 50 there may or
 * may not be a 51st. The panel says "if there are more" rather than claiming
 * either way.
 *
 * ponytail: selecting `pageInfo` on that shared document would make this
 * knowable, at the cost of editing another feature's operation and the typed
 * fixtures built against it. Worth doing when a second screen needs the same
 * answer.
 */
const WORKSPACE_CONTEXT_PROJECT_PAGE = 50

export interface InitiativeDetailPanelProps {
  /** The row that was selected, which the list already has in hand. */
  summary: Initiative
  /** The same initiative with its updates, once the detail query answers. */
  detail: InitiativeDetail | null
  isLoading: boolean
  isNotFound: boolean
  errorMessage: string | null
  onRetry: () => void

  /** Everyone in the workspace, for naming an owner and an update's author. */
  members: readonly WorkspaceMember[]
  /** The projects the workspace context could name -- at most its first 50. */
  projects: readonly WorkspaceProject[]
  /** Every loaded initiative, for the parent picker. */
  initiatives: readonly Initiative[]

  isSaving: boolean
  onAddProject: (projectId: string) => void
  onRemoveProject: (projectId: string) => void
  onSetParent: (parentInitiativeId: string) => void
  onClearParent: () => void
  onPostUpdate: (health: Health, body: string) => void
}

/**
 * Everything about one initiative that is not a list row.
 *
 * ## Where each value comes from, and what is missing
 *
 * `ownerId`, `projectIds`, `parentInitiativeId` and an update's `authorId`
 * are all raw UUIDs: the schema exposes no `Initiative.owner`, no
 * `Initiative.projects` and no `InitiativeUpdate.author`. Every name on this
 * panel is therefore resolved client-side against the workspace context, and
 * every id that cannot be resolved is SAID rather than dropped. A project
 * outside the context's first 50 shows as "a project this screen cannot
 * name", not as a shorter list -- because a shorter list is a claim about
 * what the initiative contains, and it would be false.
 *
 * ## Two sources for one initiative
 *
 * `summary` is the row from the list; `detail` is the same initiative with
 * its `updates`. The panel renders from `summary` immediately and fills the
 * updates in when they arrive, so selecting a row does not blank the fields
 * that are already known.
 */
export function InitiativeDetailPanel({
  summary,
  detail,
  isLoading,
  isNotFound,
  errorMessage,
  onRetry,
  members,
  projects,
  initiatives,
  isSaving,
  onAddProject,
  onRemoveProject,
  onSetParent,
  onClearParent,
  onPostUpdate,
}: InitiativeDetailPanelProps) {
  const fieldId = useId()
  const paths = useAppPaths()

  const [projectToAdd, setProjectToAdd] = useState('')
  const [parentToSet, setParentToSet] = useState('')
  const [updateHealth, setUpdateHealth] = useState<Health>('ON_TRACK')
  const [updateBody, setUpdateBody] = useState('')

  const memberById = new Map(members.map((member) => [member.userId, member]))
  const initiativeById = new Map(initiatives.map((entry) => [entry.id, entry]))

  const owner = summary.ownerId === null ? null : memberById.get(summary.ownerId)
  const held = resolveProjects(summary.projectIds, projects)
  const addable = addableProjects(summary.projectIds, projects)
  const candidates = parentCandidates(summary.id, initiatives)

  const parent =
    summary.parentInitiativeId === null
      ? null
      : (initiativeById.get(summary.parentInitiativeId) ?? null)

  return (
    <InspectorPanel
      label={`Initiative ${summary.name}`}
      header={
        <div className={styles.panelHeader}>
          <h2 className={styles.panelTitle}>{summary.name}</h2>
          <span className={styles.panelBadges}>
            <Badge tone={initiativeStatusTone(summary.status)}>
              {initiativeStatusLabel(summary.status)}
            </Badge>
            {summary.health === null ? (
              <Badge tone="neutral">{HEALTH_UNREPORTED}</Badge>
            ) : (
              <Badge tone={healthTone(summary.health)}>{healthLabel(summary.health)}</Badge>
            )}
          </span>
        </div>
      }
    >
      {errorMessage !== null && (
        <ErrorState
          title="Could not load this initiative"
          description={errorMessage}
          onRetry={onRetry}
        />
      )}

      {errorMessage === null && isNotFound && (
        <EmptyState
          title="This initiative is no longer available"
          description="It may have been deleted. Refresh the list to see what is left."
        />
      )}

      {errorMessage === null && !isNotFound && (
        <>
          {summary.description !== null && summary.description.trim() !== '' && (
            <p className={styles.panelProse}>{summary.description}</p>
          )}

          <dl className={styles.facts}>
            <div className={styles.fact}>
              <dt>Owner</dt>
              <dd>
                {owner === undefined || owner === null ? (
                  <span className={styles.unresolved}>
                    {summary.ownerId === null ? 'Nobody yet' : 'Someone this screen cannot name'}
                  </span>
                ) : (
                  memberLabel(owner)
                )}
              </dd>
            </div>

            <div className={styles.fact}>
              <dt>Target date</dt>
              <dd>
                {summary.targetDate === null ? (
                  <span className={styles.unresolved}>No target date</span>
                ) : (
                  <time dateTime={summary.targetDate}>{formatDay(summary.targetDate)}</time>
                )}
              </dd>
            </div>
          </dl>

          {/* ------------------------------------------------ parent */}
          <section aria-labelledby={`${fieldId}-parent-heading`} className={styles.section}>
            <h3 className={styles.sectionTitle} id={`${fieldId}-parent-heading`}>
              Part of
            </h3>

            {summary.parentInitiativeId === null ? (
              <p className={styles.sectionNote}>
                A top-level initiative. It is not nested under another.
              </p>
            ) : (
              <p className={styles.sectionNote}>
                {parent === null ? (
                  <span className={styles.unresolved}>
                    Nested under an initiative that is not on this page. Load more to see it.
                  </span>
                ) : (
                  parent.name
                )}{' '}
                <Button
                  disabled={isSaving}
                  onClick={onClearParent}
                  size="sm"
                  type="button"
                  variant="ghost"
                >
                  Move to top level
                </Button>
              </p>
            )}

            {candidates.length > 0 && (
              <div className={styles.inlineForm}>
                <label className={styles.label} htmlFor={`${fieldId}-parent`}>
                  Nest under
                </label>
                <Select
                  id={`${fieldId}-parent`}
                  onChange={(event) => {
                    setParentToSet(event.target.value)
                  }}
                  size="sm"
                  value={parentToSet}
                >
                  <option value="">Choose an initiative…</option>
                  {candidates.map((candidate) => (
                    <option key={candidate.id} value={candidate.id}>
                      {candidate.name}
                    </option>
                  ))}
                </Select>
                <Button
                  disabled={isSaving || parentToSet === ''}
                  onClick={() => {
                    onSetParent(parentToSet)
                    setParentToSet('')
                  }}
                  size="sm"
                  type="button"
                >
                  Nest
                </Button>
              </div>
            )}
          </section>

          {/* ----------------------------------------------- projects */}
          <section aria-labelledby={`${fieldId}-projects-heading`} className={styles.section}>
            <h3 className={styles.sectionTitle} id={`${fieldId}-projects-heading`}>
              Projects ({held.length})
            </h3>

            {held.length === 0 && (
              <p className={styles.sectionNote}>No projects in this initiative yet.</p>
            )}

            {held.length > 0 && (
              <ul className={styles.chips}>
                {held.map(({ projectId, project }) => (
                  <li className={styles.chip} key={projectId}>
                    {project === null ? (
                      /*
                        A real membership whose name is not in hand. Rendered
                        as what it is: the alternative -- leaving it out --
                        would report an initiative of six projects as one of
                        five.
                      */
                      <span className={styles.unresolved}>
                        A project this screen cannot name
                      </span>
                    ) : (
                      <Link className={styles.chipLink} to={paths.project(projectId)}>
                        {project.name}
                        <span className={styles.chipMeta}>
                          {projectStateLabel(project.state)}
                        </span>
                      </Link>
                    )}
                    <Button
                      disabled={isSaving}
                      onClick={() => {
                        onRemoveProject(projectId)
                      }}
                      size="sm"
                      type="button"
                      variant="ghost"
                    >
                      <VisuallyHidden>
                        {`Remove ${project === null ? 'this project' : project.name} from ${summary.name}`}
                      </VisuallyHidden>
                      <span aria-hidden="true">×</span>
                    </Button>
                  </li>
                ))}
              </ul>
            )}

            {addable.length > 0 && (
              <div className={styles.inlineForm}>
                <label className={styles.label} htmlFor={`${fieldId}-project`}>
                  Add a project
                </label>
                <Select
                  id={`${fieldId}-project`}
                  onChange={(event) => {
                    setProjectToAdd(event.target.value)
                  }}
                  size="sm"
                  value={projectToAdd}
                >
                  <option value="">Choose a project…</option>
                  {addable.map((project) => (
                    <option key={project.id} value={project.id}>
                      {project.name}
                    </option>
                  ))}
                </Select>
                <Button
                  disabled={isSaving || projectToAdd === ''}
                  onClick={() => {
                    onAddProject(projectToAdd)
                    setProjectToAdd('')
                  }}
                  size="sm"
                  type="button"
                >
                  Add
                </Button>
              </div>
            )}

            {projects.length >= WORKSPACE_CONTEXT_PROJECT_PAGE && (
              <p className={styles.sectionNote}>
                This picker lists the {WORKSPACE_CONTEXT_PROJECT_PAGE} projects the
                workspace context loaded. If the workspace has more, they are not offered
                here.
              </p>
            )}
          </section>

          {/* ------------------------------------------------ updates */}
          <section aria-labelledby={`${fieldId}-updates-heading`} className={styles.section}>
            <h3 className={styles.sectionTitle} id={`${fieldId}-updates-heading`}>
              Updates
            </h3>

            <div className={styles.updateForm}>
              <div className={styles.field}>
                <label className={styles.label} htmlFor={`${fieldId}-health`}>
                  Health
                </label>
                <Select
                  id={`${fieldId}-health`}
                  onChange={(event) => {
                    setUpdateHealth(event.target.value as Health)
                  }}
                  size="sm"
                  value={updateHealth}
                >
                  {HEALTHS.map((value) => (
                    <option key={value} value={value}>
                      {healthLabel(value)}
                    </option>
                  ))}
                </Select>
              </div>

              <div className={styles.field}>
                <label className={styles.label} htmlFor={`${fieldId}-body`}>
                  What changed
                </label>
                <Textarea
                  id={`${fieldId}-body`}
                  onChange={(event) => {
                    setUpdateBody(event.target.value)
                  }}
                  rows={3}
                  value={updateBody}
                />
              </div>

              <div className={styles.formActions}>
                <Button
                  disabled={isSaving || updateBody.trim() === ''}
                  onClick={() => {
                    onPostUpdate(updateHealth, updateBody.trim())
                    setUpdateBody('')
                  }}
                  type="button"
                  variant="primary"
                >
                  Post update
                </Button>
              </div>
            </div>

            {isLoading && (
              <div className={styles.skeletonStack} role="status" aria-busy="true">
                <VisuallyHidden as="div">Loading updates</VisuallyHidden>
                {Array.from({ length: 2 }, (_unused, index) => (
                  <Skeleton key={index} width="100%" height="3rem" />
                ))}
              </div>
            )}

            {!isLoading && detail !== null && detail.updates.length === 0 && (
              <p className={styles.sectionNote}>
                Nobody has reported on this initiative yet, which is why it has no health.
              </p>
            )}

            {!isLoading && detail !== null && detail.updates.length > 0 && (
              <ol className={styles.updates}>
                {detail.updates.map((update) => {
                  const author = memberById.get(update.authorId)

                  return (
                    <li className={styles.update} key={update.id}>
                      <div className={styles.updateHead}>
                        <Badge tone={healthTone(update.health)}>
                          {healthLabel(update.health)}
                        </Badge>
                        <span className={styles.updateMeta}>
                          {author === undefined ? (
                            <span className={styles.unresolved}>Someone not in this list</span>
                          ) : (
                            memberLabel(author)
                          )}
                          {' · '}
                          <time dateTime={update.createdAt}>
                            {formatRelative(update.createdAt)}
                          </time>
                        </span>
                      </div>
                      <p className={styles.updateBody}>{update.body}</p>
                    </li>
                  )
                })}
              </ol>
            )}
          </section>
        </>
      )}
    </InspectorPanel>
  )
}
