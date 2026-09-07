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
import type { ProjectIssue, ProjectMilestone, ProjectUnfiledIssue } from '../api'
import { closedCount } from '../lib/projects'
import styles from '../projects.module.css'

export interface ProjectIssuesProps {
  projectId: string
  /** The issues in this project, as far as the server has been paged for them. */
  issues: readonly ProjectIssue[]
  /** How many are in the project altogether. */
  totalCount: number
  /** The issues in no project, which are what the "add" menu offers. */
  unfiledIssues: readonly ProjectUnfiledIssue[]
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
 * `filter: { projectId }` decides membership on the server, so this renders
 * what it was given rather than sifting a page of the workspace for it. What
 * survives from that arrangement is one honest sentence: the list is still
 * paginated, so while `hasNextPage` the panel says how many of the project's
 * issues are on screen. Once they are all here it says nothing.
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
  totalCount,
  unfiledIssues,
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

  const addItems: readonly MenuItem[] = unfiledIssues.map((issue) => ({
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
            label="Add an unfiled issue to this project"
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
                  label={
                    hasNextPage
                      ? 'Closed issues in this project, among those loaded'
                      : 'Closed issues in this project'
                  }
                  showLabel
                />
                <span>closed &middot; completed or canceled</span>
              </p>
            )}

            {issues.length === 0 ? (
              <EmptyState
                icon={<IssuesIcon />}
                title="No issues in this project yet"
                description="Nothing in this workspace is filed against this project."
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

            {/* Only while the answer is partial. A count that matches what is
                on screen is a sentence nobody needs to read. */}
            {hasNextPage && (
              <>
                <p className={styles.footnote} role="status">
                  Showing {issues.length} of {totalCount} issues in this project.
                </p>

                <Button size="sm" onClick={onLoadMore} loading={isLoadingMore}>
                  Load more
                </Button>
              </>
            )}
          </>
        )}
      </div>
    </section>
  )
}
