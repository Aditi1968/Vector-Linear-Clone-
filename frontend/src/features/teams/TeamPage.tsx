import { Link } from 'react-router-dom'

import { PageContent, PageHeader } from '../../app/layout'
import { useAppPaths } from '../../app/routes'
import {
  Badge,
  EmptyState,
  ErrorState,
  IssuesIcon,
  List,
  ListRow,
  ListRowMain,
  ListRowMeta,
  Spinner,
  StatusIndicator,
  Tag,
  statusCategoryFrom,
} from '../../components'
import { useWorkspaceContext } from '../issues/api'
import { formatAbsolute } from '../issues/lib/dates'
import { IssueRows } from '../screens'
import styles from '../screens.module.css'
import { useTeamByKey, useTeamIssues } from './api'
import type { TeamWorkflowState } from './api'
import { TeamStateScreen } from './TeamStates'

/** How many issues the overview previews before sending you to the full list. */
const PREVIEW_ROWS = 5

/**
 * What a workflow state's category means, in the product's own words.
 *
 * Keyed by the schema's enum rather than switched on, so a category added to
 * `WorkflowStateCategory` fails to compile here instead of rendering as a
 * blank chip. The names a team gives its states are its own; these five are
 * the fixed meanings behind them.
 */
const CATEGORY_LABELS: Record<TeamWorkflowState['category'], string> = {
  BACKLOG: 'Backlog',
  UNSTARTED: 'Unstarted',
  STARTED: 'Started',
  COMPLETED: 'Completed',
  CANCELED: 'Canceled',
}

/**
 * One team's overview: its board, and the work on it.
 *
 * ## Addressed by key
 *
 * The URL is `/:workspaceSlug/team/ENG`, because ENG is the name the team is
 * known by -- it is the prefix of every one of its issue identifiers. No root
 * field takes a key, so `useTeamByKey` resolves it over
 * `teams(workspaceSlug:)` and reports "no such team" as its own state rather
 * than as an empty screen. See ./api.ts.
 *
 * ## Why the workflow states are the centre of this screen
 *
 * A team in this product *is* its board. `Team.workflowStates` is the only
 * thing `Team` carries beyond a name and a key, and the states are what
 * decide what an issue on this team can be -- so they are listed in their
 * server order, with the fixed category behind each name, because a team may
 * call a state anything and the category is what the rest of the product
 * branches on.
 *
 * The issues are a preview and say so. The full list is its own screen,
 * against the same query and the same cache entry, so following the link
 * costs no request.
 *
 * `AppLayout` owns the `<main>` landmark and `PageHeader` owns the page's
 * only `<h1>`; this renders a fragment and adds neither.
 */
export function TeamPage() {
  const paths = useAppPaths()
  const resolution = useTeamByKey()

  const teamId = resolution.status === 'found' ? resolution.team.id : null

  // Called unconditionally and skipped internally while the key is still
  // resolving: a hook cannot sit behind an early return.
  const { issues, hasNextPage, isLoadingFirstPage, errorMessage } = useTeamIssues(teamId)

  // The lookups a row needs to draw a status glyph and an avatar from the
  // UUIDs it carries. A failure costs those columns and nothing else.
  const { stateById, memberById } = useWorkspaceContext()

  if (resolution.status !== 'found') {
    return <TeamStateScreen resolution={resolution} />
  }

  const { team } = resolution
  const states = [...team.workflowStates].sort((a, b) => a.position - b.position)
  const preview = issues.slice(0, PREVIEW_ROWS)

  return (
    <>
      <PageHeader
        title={team.name}
        description={`Team ${team.key} · created ${formatAbsolute(team.createdAt)}`}
        actions={<Link to={paths.teamIssues(team.key)}>All {team.key} issues</Link>}
      />

      <PageContent>
        <div className={styles.split}>
          <section className={styles.panel} aria-labelledby="team-issues">
            <div className={styles.panelHeader}>
              <h2 className={styles.panelTitle} id="team-issues">
                Recent issues
              </h2>
              <Link to={paths.teamIssues(team.key)}>View all</Link>
            </div>

            <div className={styles.panelBody}>
              {isLoadingFirstPage && <Spinner label={`Loading ${team.key} issues`} />}

              {!isLoadingFirstPage && errorMessage !== null && (
                <p className={styles.formError} role="alert">
                  {errorMessage}
                </p>
              )}

              {!isLoadingFirstPage && errorMessage === null && preview.length === 0 && (
                <EmptyState
                  icon={<IssuesIcon />}
                  title="No issues on this team yet"
                  description={`The first issue filed against ${team.key} will appear here.`}
                />
              )}

              {preview.length > 0 && (
                <>
                  <IssueRows
                    label={`Recent ${team.key} issues`}
                    issues={preview}
                    stateById={stateById}
                    memberById={memberById}
                  />
                  {/* "Newest" and not "all": this is a preview, and a list
                      that quietly showed five of forty would be read as
                      forty. The full list is one link away. */}
                  <p className={styles.footnote}>
                    The {preview.length} newest of this team&rsquo;s issues.
                    {hasNextPage || issues.length > preview.length
                      ? ' There are more.'
                      : ''}
                  </p>
                </>
              )}
            </div>
          </section>

          <section className={styles.panel} aria-labelledby="team-states">
            <div className={styles.panelHeader}>
              <h2 className={styles.panelTitle} id="team-states">
                Workflow states
              </h2>
            </div>

            <div className={styles.panelBody}>
              {states.length === 0 ? (
                /* Every team is seeded with the default states at creation, so
                   this is close to unreachable -- and it is still a state and
                   not a crash, because a board with nothing on it is a fact
                   about the data and not a failure of this screen. */
                <ErrorState
                  title="This team has no workflow states"
                  description="An issue cannot be filed against a team with no states. This usually means the team was created outside Vector."
                />
              ) : (
                <>
                  <List label={`${team.key} workflow states`}>
                    {states.map((state) => {
                      const category = statusCategoryFrom(state.category)

                      return (
                        <ListRow key={state.id}>
                          <ListRowMain>
                            {/* The glyph draws a different silhouette per
                                category, so the meaning survives greyscale and
                                does not depend on the team's colour choice. */}
                            {category !== null && (
                              <StatusIndicator category={category} name={state.name} />
                            )}{' '}
                            {state.name}
                          </ListRowMain>

                          <ListRowMeta>
                            {/* The chip carries the category as text and the
                                team's own colour as its dot -- the colour is
                                data the team chose, never the only signal. */}
                            <Tag
                              name={CATEGORY_LABELS[state.category]}
                              color={state.color ?? undefined}
                            />
                            <Badge>{state.position}</Badge>
                          </ListRowMeta>
                        </ListRow>
                      )
                    })}
                  </List>

                  <p className={styles.footnote}>
                    In board order. The name belongs to the team; the category
                    beside it is the fixed meaning the rest of Vector reads.
                  </p>
                </>
              )}
            </div>
          </section>
        </div>
      </PageContent>
    </>
  )
}
