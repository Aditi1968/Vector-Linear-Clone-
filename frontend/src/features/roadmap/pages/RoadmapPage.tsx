import { Link } from 'react-router-dom'

import { PageContent, PageHeader } from '../../../app/layout'
import { useAppPaths } from '../../../app/routes'
import {
  Badge,
  Button,
  CycleIcon,
  EmptyState,
  ErrorState,
  Skeleton,
  VisuallyHidden,
} from '../../../components'
import {
  HEALTH_UNREPORTED,
  healthLabel,
  healthTone,
} from '../../initiatives/lib/initiatives'
import { formatDay } from '../../projects/lib/projects'
import { useRoadmap } from '../api'
import type { RoadmapItem } from '../lib/roadmap'
import { groupByMonth, isOverdue, toRoadmapItems } from '../lib/roadmap'
import styles from '../roadmap.module.css'

/**
 * The roadmap: every initiative and project that named a date, by month.
 *
 * ## Why there are no bars
 *
 * There is nothing to draw a bar between. `Initiative.targetDate` and
 * `Project.targetDate` are the only scheduling fields the schema has --
 * migrations 009 and 022 store one `target_date DATE` each -- so the API can
 * say when something is DUE and nothing at all about when it starts, how long
 * it takes, or how far along it is.
 *
 * A Gantt chart over that would have to invent a start. The only candidate is
 * `createdAt`, the moment the row was inserted, which is not when the work
 * began and is routinely months out. A bar from there would look like
 * information and be a drawing of a database timestamp, so this screen does
 * not draw one. What it draws instead is what the data supports: each item as
 * a marker in the month it is due, in date order, with the ones that have
 * slipped flagged and the ones nobody has scheduled listed rather than
 * hidden.
 *
 * The gap is real and is the backend's to close: a `startDate` on both types
 * would turn this screen into a timeline without changing anything else here.
 *
 * ## Why the months are not contiguous
 *
 * Only months holding something get a column. See ../lib/roadmap.ts.
 */
export function RoadmapPage() {
  const paths = useAppPaths()

  const {
    initiatives,
    projects,
    hasMore,
    isLoadingFirstPage,
    isLoadingMore,
    errorMessage,
    loadMoreErrorMessage,
    loadMore,
    retry,
  } = useRoadmap()

  const items = toRoadmapItems(initiatives, projects)
  const { months, undated } = groupByMonth(items)

  const renderItem = (item: RoadmapItem) => {
    const overdue = isOverdue(item)

    return (
      <li className={styles.item} key={`${item.kind}-${item.id}`}>
        <div className={styles.itemHead}>
          <span className={styles.itemKind}>
            {item.kind === 'initiative' ? 'Initiative' : 'Project'}
          </span>
          {item.targetDate !== null && (
            <time className={styles.itemDate} dateTime={item.targetDate}>
              {formatDay(item.targetDate)}
            </time>
          )}
        </div>

        {/*
          A project has a detail route and an initiative does not -- there is
          no `initiatives/:id` segment -- so only one of the two can be a
          link. The other is text rather than an `<a>` pointing at the list,
          because a link that does not go where its name says is worse than
          no link.
        */}
        {item.kind === 'project' ? (
          <Link className={styles.itemLink} to={paths.project(item.id)}>
            {item.name}
          </Link>
        ) : (
          <span className={styles.itemName}>{item.name}</span>
        )}

        <div className={styles.itemBadges}>
          <Badge tone={item.stateTone}>{item.stateLabel}</Badge>
          {item.health === null ? (
            <Badge tone="neutral">{HEALTH_UNREPORTED}</Badge>
          ) : (
            <Badge tone={healthTone(item.health)}>{healthLabel(item.health)}</Badge>
          )}
          {overdue && <Badge tone="danger">Past its target date</Badge>}
        </div>
      </li>
    )
  }

  return (
    <>
      <PageHeader
        title="Roadmap"
        description="Initiatives and projects in the month each is due."
      />

      <PageContent>
        {isLoadingFirstPage && (
          <div className={styles.skeletonStack} role="status" aria-busy="true">
            <VisuallyHidden as="div">Loading the roadmap</VisuallyHidden>
            {Array.from({ length: 3 }, (_unused, index) => (
              <Skeleton key={index} width="100%" height="6rem" />
            ))}
          </div>
        )}

        {!isLoadingFirstPage && errorMessage !== null && (
          <ErrorState
            title="Could not load the roadmap"
            description={errorMessage}
            onRetry={retry}
          />
        )}

        {!isLoadingFirstPage && errorMessage === null && items.length === 0 && (
          <EmptyState
            icon={<CycleIcon />}
            title="Nothing to schedule yet"
            description="The roadmap draws initiatives and projects against the month each is due. Create one of either, give it a target date, and it appears here."
          />
        )}

        {!isLoadingFirstPage && errorMessage === null && items.length > 0 && (
          <>
            {/*
              Said once, at the top, rather than left to be inferred from a
              chart that looks like a timeline and is not one.
            */}
            <p className={styles.axisNote}>
              Vector stores one date per initiative and project — the day it is due. There
              is no start date in the schema, so each item below is a marker in the month
              it is due rather than a bar across a span.
            </p>

            {months.length > 0 && (
              <div className={styles.lanes}>
                {months.map((month) => (
                  <section
                    aria-labelledby={`roadmap-month-${month.key}`}
                    className={styles.lane}
                    key={month.key}
                  >
                    <h2 className={styles.laneTitle} id={`roadmap-month-${month.key}`}>
                      {month.label}
                      <span className={styles.laneCount}>
                        {month.items.length}{' '}
                        {month.items.length === 1 ? 'item' : 'items'}
                      </span>
                    </h2>
                    <ul className={styles.items}>{month.items.map(renderItem)}</ul>
                  </section>
                ))}
              </div>
            )}

            {undated.length > 0 && (
              <section aria-labelledby="roadmap-undated" className={styles.undated}>
                <h2 className={styles.laneTitle} id="roadmap-undated">
                  Not scheduled
                  <span className={styles.laneCount}>
                    {undated.length} {undated.length === 1 ? 'item' : 'items'}
                  </span>
                </h2>
                <p className={styles.axisNote}>
                  These have no target date, so there is no month to put them in. They are
                  listed rather than left off — an unscheduled project is the thing a
                  roadmap most needs to point at.
                </p>
                <ul className={styles.items}>{undated.map(renderItem)}</ul>
              </section>
            )}

            {loadMoreErrorMessage !== null && (
              <p className={styles.actionError} role="alert">
                {loadMoreErrorMessage}
              </p>
            )}

            {hasMore && (
              <div className={styles.loadMore}>
                {/*
                  Neither connection carries a `totalCount`, so "of N" is a
                  number nobody can supply. What can be said -- that the
                  calendar is incomplete -- is said.
                */}
                <p className={styles.countLine}>
                  Drawn from{' '}
                  <span className={styles.count}>{initiatives.length}</span> initiatives
                  and <span className={styles.count}>{projects.length}</span> projects.
                  There are more, so this is not the whole roadmap yet.
                </p>
                <Button disabled={isLoadingMore} onClick={loadMore} variant="secondary">
                  {isLoadingMore ? 'Loading…' : 'Load more'}
                </Button>
              </div>
            )}
          </>
        )}
      </PageContent>
    </>
  )
}
