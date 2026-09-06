import {
  Badge,
  Button,
  EmptyState,
  IssuesIcon,
  List,
  ListRow,
  ListRowMain,
  ListRowMeta,
  Menu,
  PlusIcon,
  ProgressIndicator,
  Spinner,
} from '../../../components'
import type { MenuItem } from '../../../components'
import type { ProjectIssue, ProjectMilestone } from '../api'
import { closedCount } from '../lib/projects'
import styles from '../projects.module.css'

/**
 * How many unassigned issues the "add an issue" menu offers.
 *
 * ponytail: a capped menu, not a searchable picker. The menu is built from
 * the issues this screen has already loaded, so a cap is what keeps it from
 * becoming a scrolling list of everything in the workspace. Replace with a
 * search field once the backend can filter issues server-side -- at which
 * point this whole component stops filtering client-side too.
 */
const ADDABLE_LIMIT = 15

export interface ProjectIssuesProps {
  projectId: string
  /** The issues in this project, already filtered by the page. */
  issues: readonly ProjectIssue[]
  /** Every issue loaded, filtered or not. The "add" menu is built from these. */
  loadedIssues: readonly ProjectIssue[]
  milestones: readonly ProjectMilestone[]
  isLoading: boolean
  isLoadingMore: boolean
  hasNextPage: boolean
  errorMessage: string | null
  isSaving: boolean
  onLoadMore: () => void
  /** Move an issue into this project, into a milestone, or out of the project. */
  onPlace: (issueId: string, projectId: string | null, milestoneId: string | null) => void
}

/**
 * A project's issues.
 *
 * ## The constraint this component is shaped by
 *
 * There is no `project.issues` field and no `issues(projectId:)` argument.
 * The API can be asked for a page of the *workspace's* issues and nothing
 * narrower, so "in this project" is decided here, in the browser, over
 * whatever has been loaded.
 *
 * That is stated on screen rather than papered over. The footnote says what
 * the list is, "Load more" says it loads more of the workspace rather than
 * more of this project, and the progress indicator is labelled as counting
 * loaded issues. A panel that quietly showed a partial list as a complete one
 * would be the same UI with a lie in it.
 *
 * ## Progress
 *
 * `completedAt` is the only completion signal the selection carries, and the
 * schema is explicit that it is non-null for canceled issues as well as
 * completed ones -- so this counts *closed* issues and says so. Counting a
 * canceled issue as done would overstate progress, which is the one direction
 * a progress bar must never be wrong in.
 */
export function ProjectIssues({
  projectId,
  issues,
  loadedIssues,
  milestones,
  isLoading,
  isLoadingMore,
  hasNextPage,
  errorMessage,
  isSaving,
  onLoadMore,
  onPlace,
}: ProjectIssuesProps) {
  const closed = closedCount(issues)

  const addable = loadedIssues
    .filter((issue) => issue.projectId === null)
    .slice(0, ADDABLE_LIMIT)

  const addItems: readonly MenuItem[] = addable.map((issue) => ({
    id: issue.id,
    label: `${issue.identifier} ${issue.title}`,
    disabled: isSaving,
    onSelect: () => {
      onPlace(issue.id, projectId, null)
    },
  }))

  return (
    <section className={styles.panel} aria-labelledby="project-issues">
      <div className={styles.panelHeader}>
        <h2 className={styles.panelTitle} id="project-issues">
          Issues
        </h2>

        {addItems.length > 0 && (
          <Menu
            label="Add a loaded issue to this project"
            items={addItems}
            icon={<PlusIcon />}
            size="sm"
            align="end"
          >
            Add issue
          </Menu>
        )}
      </div>

      <div className={styles.panelBody}>
        {isLoading && <Spinner label="Loading issues" />}

        {errorMessage !== null && (
          <p className={styles.formError} role="alert">
            {errorMessage}
          </p>
        )}

        {!isLoading && errorMessage === null && (
          <>
            {issues.length > 0 && (
              <p className={styles.progressRow}>
                <ProgressIndicator
                  value={closed}
                  total={issues.length}
                  label="Closed issues in this project, among those loaded"
                  showLabel
                />
                <span>closed &middot; completed or canceled</span>
              </p>
            )}

            {issues.length === 0 ? (
              <EmptyState
                icon={<IssuesIcon />}
                title="No issues in this project yet"
                description={
                  hasNextPage
                    ? 'None among the issues loaded so far. Older issues may belong to it -- load more below.'
                    : 'Every issue in this workspace has been checked; none is filed against this project.'
                }
              />
            ) : (
              <List label="Issues in this project">
                {issues.map((issue) => {
                  const items: readonly MenuItem[] = [
                    ...milestones.map((milestone) => ({
                      id: milestone.id,
                      label:
                        issue.milestoneId === milestone.id
                          ? `${milestone.name} (current)`
                          : `Move to ${milestone.name}`,
                      disabled: isSaving || issue.milestoneId === milestone.id,
                      onSelect: () => {
                        onPlace(issue.id, projectId, milestone.id)
                      },
                    })),
                    {
                      id: 'no-milestone',
                      label: 'No milestone',
                      disabled: isSaving || issue.milestoneId === null,
                      separatorBefore: milestones.length > 0,
                      onSelect: () => {
                        onPlace(issue.id, projectId, null)
                      },
                    },
                    {
                      id: 'remove',
                      label: 'Remove from project',
                      destructive: true,
                      disabled: isSaving,
                      separatorBefore: true,
                      onSelect: () => {
                        // Both null: an issue with no project cannot be in
                        // one of its milestones, and the server would reject
                        // a milestone without its project anyway.
                        onPlace(issue.id, null, null)
                      },
                    },
                  ]

                  return (
                    <ListRow key={issue.id}>
                      <ListRowMain>
                        {issue.identifier} {issue.title}
                      </ListRowMain>
                      <ListRowMeta>
                        {issue.completedAt !== null && <Badge tone="success">Closed</Badge>}
                        <Menu
                          label={`Actions for ${issue.identifier}`}
                          items={items}
                          size="sm"
                          align="end"
                        />
                      </ListRowMeta>
                    </ListRow>
                  )
                })}
              </List>
            )}

            <p className={styles.footnote}>
              The API offers no per-project issue filter, so this list is
              matched in the browser against the {loadedIssues.length} most
              recent {loadedIssues.length === 1 ? 'issue' : 'issues'} in the
              workspace. Loading more loads more of the workspace, not more of
              this project.
            </p>

            {hasNextPage && (
              <Button size="sm" onClick={onLoadMore} loading={isLoadingMore}>
                Load more workspace issues
              </Button>
            )}
          </>
        )}
      </div>
    </section>
  )
}
