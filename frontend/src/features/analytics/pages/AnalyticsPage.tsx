import { PageContent, PageHeader } from '../../../app/layout'
import {
  BoardIcon,
  EmptyState,
  ErrorState,
  ProgressIndicator,
  Select,
  Skeleton,
  VisuallyHidden,
} from '../../../components'
import { MAX_RANGE_DAYS, RANGE_OPTIONS, useWorkspaceAnalytics } from '../api'
import type { RangeDays } from '../api'
import { BreakdownTable } from '../components/BreakdownTable'
import { ThroughputChart } from '../components/ThroughputChart'
import {
  denseStateMix,
  densePriorityMix,
  describeCycleTimeCoverage,
  describeEstimateTotal,
  describeSample,
  describeWindow,
  formatHours,
  formatRate,
  hasMixedScales,
} from '../lib/metrics'
import styles from '../analytics.module.css'

/**
 * How this workspace has been moving, over a bounded recent window.
 *
 * ## Every number here is queried, none is derived from a page of issues
 *
 * This screen was a placeholder for as long as it was because the schema had
 * no aggregate to read, and the alternative -- fetching one page of issues and
 * computing "the workspace's throughput" from it -- would have been a chart of
 * fifty rows presented as a chart of everything. `workspaceAnalytics` answers
 * from `GROUP BY` over the whole tenant, so the figures are the workspace's.
 *
 * ## What is missing is on the screen, not left to be inferred
 *
 * Three things this schema cannot say honestly, and each is stated where it
 * would otherwise be assumed:
 *
 *   * **Cycle time is partial.** There is no `started_at` column, so the start
 *     of work is read from the activity history -- which begins at one
 *     migration and records nothing for an issue dragged straight to done.
 *     The coverage is printed beside the figure and the figure is suppressed
 *     entirely when nothing could be measured.
 *   * **There is no workspace-wide estimate total.** Teams choose their own
 *     scale, and points, hours and t-shirt sizes have no common unit -- a
 *     t-shirt estimate is a rung on a ladder, so a sum of two is not a size.
 *     Estimates are shown per team and per cycle with the unit printed, and
 *     the screen says why there is no total when the scales differ.
 *   * **Days are UTC.** Nothing in the schema records a viewer's timezone, so
 *     a completion at 23:00 in Los Angeles lands on the next day's bar.
 *
 * ## No charting library
 *
 * Three series over 180 points and four horizontal bar charts, drawn as SVG in
 * ../components. Every colour is a token, so the charts follow the theme.
 *
 * ## One control
 *
 * The range picker, and nothing else -- plus the retry button that only exists
 * in the error state, which is never on screen at the same time. Every chart's
 * text alternative is the table it is drawn inside, so there is no "show as
 * table" disclosure per chart and therefore no set of controls sharing an
 * accessible name.
 */
export function AnalyticsPage() {
  const {
    analytics,
    days,
    setDays,
    isLoadingFirstPage,
    isRefetching,
    errorMessage,
    retry,
  } = useWorkspaceAnalytics()

  const rangePicker = (
    <Select
      aria-label="Date range"
      value={String(days)}
      onChange={(event) => {
        setDays(Number(event.target.value) as RangeDays)
      }}
    >
      {RANGE_OPTIONS.map((option) => (
        <option key={option} value={String(option)}>
          Last {option} days
        </option>
      ))}
    </Select>
  )

  return (
    <>
      <PageHeader
        title="Analytics"
        description="Throughput, cycle time, and how the workspace is actually moving."
        actions={rangePicker}
      />

      <PageContent>
        {isLoadingFirstPage && (
          <div className={styles.skeletonStack} role="status" aria-busy="true">
            <VisuallyHidden as="div">Loading the analytics</VisuallyHidden>
            {Array.from({ length: 3 }, (_unused, index) => (
              <Skeleton key={index} width="100%" height="8rem" />
            ))}
          </div>
        )}

        {!isLoadingFirstPage && errorMessage !== null && (
          <ErrorState
            title="Could not load the analytics"
            description={errorMessage}
            onRetry={retry}
          />
        )}

        {!isLoadingFirstPage && errorMessage === null && analytics !== null && (
          <div className={isRefetching ? styles.stale : undefined}>
            {/*
              Said once, at the top. The window is a fact about every figure
              below it, and repeating it per card would be eleven copies of one
              sentence.
            */}
            <p className={styles.note}>
              {describeWindow(analytics.rangeStart, analytics.rangeEnd, analytics.days)}{' '}
              {`The widest window this server will answer is ${MAX_RANGE_DAYS} days; a wider one is refused rather than quietly shortened.`}
            </p>

            {isRefetching && (
              <p className={styles.note} role="status">
                Loading a {days}-day window. The figures below are still the previous one.
              </p>
            )}

            <section aria-labelledby="analytics-flow" className={styles.section}>
              <h2 className={styles.sectionTitle} id="analytics-flow">
                Flow
              </h2>

              <dl className={styles.tiles}>
                <div className={styles.tile}>
                  <dt className={styles.tileLabel}>Issues created</dt>
                  <dd className={styles.tileValue}>{analytics.totals.created}</dd>
                </div>
                <div className={styles.tile}>
                  <dt className={styles.tileLabel}>Issues completed</dt>
                  <dd className={styles.tileValue}>{analytics.totals.completed}</dd>
                </div>
                <div className={styles.tile}>
                  <dt className={styles.tileLabel}>Issues canceled</dt>
                  <dd className={styles.tileValue}>{analytics.totals.canceled}</dd>
                </div>
                <div className={styles.tile}>
                  <dt className={styles.tileLabel}>Completion rate</dt>
                  <dd className={styles.tileValue}>
                    {formatRate(analytics.totals.completionRate)}
                  </dd>
                  <dd className={styles.tileNote}>
                    {analytics.totals.completionRate === null
                      ? 'Nothing stopped in this window, so there is no rate — this is not a rate of zero.'
                      : 'Of the work that stopped, the share delivered rather than abandoned.'}
                  </dd>
                </div>
                <div className={styles.tile}>
                  <dt className={styles.tileLabel}>Overdue</dt>
                  <dd className={styles.tileValue}>{analytics.overdue}</dd>
                  <dd className={styles.tileNote}>
                    Open issues past their due date, as of today. Not limited to this window.
                  </dd>
                </div>
              </dl>

              <ThroughputChart series={analytics.throughput} />

              <p className={styles.legend}>
                <span className={styles.swatchCompleted} /> Completed
                <span className={styles.swatchCanceled} /> Canceled
                <span className={styles.swatchCreated} /> Created
              </p>
              <p className={styles.note}>
                One bar per day, including the days nothing happened on — a flat stretch here
                is a real quiet week and not missing data.
              </p>
            </section>

            <section aria-labelledby="analytics-durations" className={styles.section}>
              <h2 className={styles.sectionTitle} id="analytics-durations">
                How long work takes
              </h2>

              <dl className={styles.tiles}>
                <div className={styles.tile}>
                  <dt className={styles.tileLabel}>Lead time (median)</dt>
                  <dd className={styles.tileValue}>
                    {analytics.leadTime === null
                      ? '—'
                      : formatHours(analytics.leadTime.medianHours)}
                  </dd>
                  <dd className={styles.tileNote}>
                    Created to completed. {describeSample(analytics.leadTime)}
                    {analytics.leadTime !== null &&
                      ` 90th percentile ${formatHours(analytics.leadTime.p90Hours)}.`}
                  </dd>
                </div>

                <div className={styles.tile}>
                  <dt className={styles.tileLabel}>Cycle time (median)</dt>
                  <dd className={styles.tileValue}>
                    {/*
                      Suppressed rather than shown as a zero when nothing could
                      be measured. A "0h" here would read as instant delivery,
                      which is the most flattering possible misreading of an
                      absent measurement.
                    */}
                    {analytics.cycleTime !== null &&
                    describeCycleTimeCoverage(analytics.cycleTime).isMeasurable
                      ? formatHours(analytics.cycleTime.medianHours)
                      : '—'}
                  </dd>
                  <dd className={styles.tileNote}>
                    {describeCycleTimeCoverage(analytics.cycleTime).note}
                  </dd>
                </div>

                <div className={styles.tile}>
                  <dt className={styles.tileLabel}>Open issue age (median)</dt>
                  <dd className={styles.tileValue}>
                    {analytics.issueAge === null
                      ? '—'
                      : formatHours(analytics.issueAge.medianHours)}
                  </dd>
                  <dd className={styles.tileNote}>
                    How long unfinished work has been open, as of now — a snapshot of the
                    backlog rather than of this window.
                  </dd>
                </div>
              </dl>
            </section>

            <section aria-labelledby="analytics-distribution" className={styles.section}>
              <h2 className={styles.sectionTitle} id="analytics-distribution">
                Where the work sits
              </h2>

              <div className={styles.grid}>
                <BreakdownTable
                  caption="Issues by status"
                  unitLabel="Status"
                  emptyMessage="This workspace has no issues yet."
                  rows={denseStateMix(analytics.stateMix).map((row) => ({
                    key: row.category,
                    label: row.label,
                    value: row.issues,
                  }))}
                />

                <BreakdownTable
                  caption="Open issues by priority"
                  unitLabel="Priority"
                  emptyMessage="Nothing is open, so there is no priority breakdown."
                  rows={densePriorityMix(analytics.priorityMix).map((row) => ({
                    key: String(row.priority),
                    label: row.label,
                    value: row.issues,
                  }))}
                />

                <BreakdownTable
                  caption="Open issues by assignee"
                  unitLabel="Assignee"
                  emptyMessage="Nothing is open, so nobody is carrying anything."
                  rows={analytics.workload.map((load) => ({
                    key: load.assigneeId ?? 'unassigned',
                    label: load.name ?? 'Unassigned',
                    value: load.openIssues,
                  }))}
                />

                <BreakdownTable
                  caption="Issues by project"
                  unitLabel="Project"
                  emptyMessage="No project holds any issues yet."
                  rows={analytics.projects.map((project) => ({
                    key: project.projectId,
                    label: project.name,
                    value: project.issues,
                    detail: `${project.completed} of ${project.issues} done`,
                  }))}
                />
              </div>

              <p className={styles.note}>
                {/*
                  Both tables are bounded server-side, and a bounded table that
                  does not say so reads as the whole workspace.
                */}
                Showing {analytics.workload.length} of {analytics.assigneeTotal} assignees and{' '}
                {analytics.projects.length} of {analytics.projectTotal} projects, busiest first.
                Issues in no project are not counted — most issues are in none, and that bar
                would be the largest on the chart and would say nothing. An assignee who has
                left the workspace still appears, because their work has not gone anywhere.
              </p>
            </section>

            <section aria-labelledby="analytics-delivery" className={styles.section}>
              <h2 className={styles.sectionTitle} id="analytics-delivery">
                Delivery by team
              </h2>

              {analytics.teams.length === 0 ? (
                <EmptyState
                  icon={<BoardIcon />}
                  title="Nothing was delivered in this window"
                  description="No team completed an issue between these two dates. Widen the range, or check the throughput chart above for when work last stopped."
                />
              ) : (
                <>
                  <table className={styles.table}>
                    <caption className={styles.tableCaption}>
                      What each team delivered, in its own estimate unit
                    </caption>
                    <thead>
                      <tr>
                        <th scope="col">Team</th>
                        <th scope="col">Completed</th>
                        <th scope="col">Estimated</th>
                      </tr>
                    </thead>
                    <tbody>
                      {analytics.teams.map((team) => (
                        <tr key={team.teamId}>
                          <th scope="row" className={styles.rowLabel}>
                            {team.name} <span className={styles.key}>{team.key}</span>
                          </th>
                          <td className={styles.rowValue}>{team.completed}</td>
                          <td className={styles.rowValue}>
                            {describeEstimateTotal(
                              team.estimateTotal,
                              team.estimated,
                              team.estimateScale,
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>

                  <p className={styles.note}>
                    Showing {analytics.teams.length} of {analytics.teamTotal} teams.{' '}
                    {hasMixedScales(analytics.teams)
                      ? 'These teams estimate in different units, so there is deliberately no workspace total: points, hours and t-shirt sizes have no common measure, and a t-shirt size is a rung on a ladder rather than a quantity that sums.'
                      : 'Estimates are summed within a team and never across teams, because each team chooses its own unit.'}
                  </p>
                </>
              )}
            </section>

            <section aria-labelledby="analytics-cycles" className={styles.section}>
              <h2 className={styles.sectionTitle} id="analytics-cycles">
                Cycles
              </h2>

              {analytics.cycles.length === 0 ? (
                <p className={styles.note}>
                  No cycle overlaps this window. Cycles appear here when their own start and
                  end dates fall inside the range above.
                </p>
              ) : (
                <>
                  <table className={styles.table}>
                    <caption className={styles.tableCaption}>
                      Cycle progress and velocity, over each cycle&rsquo;s whole scope
                    </caption>
                    <thead>
                      <tr>
                        <th scope="col">Cycle</th>
                        <th scope="col">Progress</th>
                        <th scope="col">Delivered</th>
                      </tr>
                    </thead>
                    <tbody>
                      {analytics.cycles.map((cycle) => {
                        const name = cycle.name ?? `Cycle ${cycle.number}`

                        return (
                          <tr key={cycle.cycleId}>
                            <th scope="row" className={styles.rowLabel}>
                              {name} <span className={styles.key}>{cycle.teamKey}</span>
                            </th>
                            <td className={styles.rowValue}>
                              <ProgressIndicator
                                label={name}
                                value={cycle.completed}
                                total={cycle.issues}
                                showLabel
                              />
                            </td>
                            <td className={styles.rowValue}>
                              {describeEstimateTotal(
                                cycle.completedEstimate,
                                cycle.estimated,
                                cycle.estimateScale,
                              )}
                            </td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>

                  <p className={styles.note}>
                    Showing {analytics.cycles.length} of {analytics.cycleTotal} cycles. Each
                    row counts the cycle&rsquo;s whole scope, not the part of it inside the
                    window — a sprint&rsquo;s progress is the sprint&rsquo;s. Scope is where
                    issues are now: Vector does not record what a cycle held on a past day, so
                    there is no burndown here rather than a smooth line drawn from today&rsquo;s
                    estimates.
                  </p>
                </>
              )}
            </section>
          </div>
        )}
      </PageContent>
    </>
  )
}
