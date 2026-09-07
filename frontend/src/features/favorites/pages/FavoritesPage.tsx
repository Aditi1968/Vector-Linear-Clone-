import { useCallback, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import { PageContent, PageHeader } from '../../../app/layout'
import { useAppPaths } from '../../../app/routes'
import {
  Badge,
  ChevronDownIcon,
  ChevronUpIcon,
  EmptyState,
  ErrorState,
  IconButton,
  List,
  ListRow,
  ListRowMain,
  ListRowMeta,
  Menu,
  Skeleton,
  StarIcon,
  VisuallyHidden,
} from '../../../components'
import type { MenuItem } from '../../../components'
import { useWorkspaceContext } from '../../issues/api'
import { useFavoriteActions, useFavoriteList, useFavoriteSavedViews } from '../api'
import type { FavoriteTarget } from '../api'
import { inServerOrder, reorderPlan, resolveFavorite } from '../lib/favorites'
import styles from '../favorites.module.css'

/**
 * The viewer's own shortcut list.
 *
 * ## Why a row has to be joined before it can be drawn
 *
 * `Favorite` is `{ id, teamId, projectId, savedViewId, position }`. It
 * carries no name and no resolved target -- the schema exposes no
 * `Favorite.team`, `Favorite.project` or `Favorite.savedView` -- so every
 * label on this screen is looked up from an id.
 *
 * Three workspace-level lists do it: teams and projects come from
 * `useWorkspaceContext()`, the issues feature's shared document that most
 * screens have already put in the cache, and saved views come from this
 * feature's own two-field query. That is three requests for the whole screen,
 * read from maps built once -- not one request per row, which is what a
 * `Favorite.name` field would have saved and what its absence must not be
 * allowed to cost.
 *
 * A target that is not in those lists resolves to no name. That is an
 * ordinary outcome rather than an error: the thing was deleted and the
 * shortcut outlived it, or it is a saved view past the first page of 50. The
 * row says so; it does not guess a name and does not disappear.
 *
 * ## Why reordering is two buttons and not a drag
 *
 * A drag needs a pointer. Two buttons are reachable by keyboard, announce
 * themselves, and work on a phone -- and `favoriteReorder` moves one row per
 * call either way, so the drag would buy nothing but exclusion. See
 * ../lib/favorites.ts for why a move can send more than one request.
 */
export function FavoritesPage() {
  const paths = useAppPaths()
  const { teams, projects, isLoading: isLoadingContext } = useWorkspaceContext()
  const savedViews = useFavoriteSavedViews()

  const { favorites, isLoading, errorMessage, retry } = useFavoriteList()
  const actions = useFavoriteActions()

  const [actionError, setActionError] = useState<string | null>(null)

  const lookups = useMemo(() => {
    const teamById = new Map(teams.map((team) => [team.id, team]))
    const projectById = new Map(projects.map((project) => [project.id, project]))
    const viewById = new Map(savedViews.map((view) => [view.id, view]))

    return {
      teamById,
      teamName: (id: string) => teamById.get(id)?.name,
      projectName: (id: string) => projectById.get(id)?.name,
      savedViewName: (id: string) => viewById.get(id)?.name,
    }
  }, [projects, savedViews, teams])

  const ordered = useMemo(() => inServerOrder(favorites), [favorites])

  const resolved = useMemo(
    () => ordered.map((favorite) => resolveFavorite(favorite, lookups)),
    [lookups, ordered],
  )

  /** What is already starred, so the add menu does not offer it twice. */
  const starred = useMemo(() => {
    const keys = new Set<string>()

    for (const favorite of favorites) {
      for (const id of [favorite.teamId, favorite.projectId, favorite.savedViewId]) {
        if (id !== null) {
          keys.add(id)
        }
      }
    }

    return keys
  }, [favorites])

  const report = useCallback(
    (outcome: Awaited<ReturnType<typeof actions.removeFavorite>>) => {
      if (outcome.status === 'ok') {
        setActionError(null)
        return
      }

      setActionError(
        outcome.status === 'failed'
          ? outcome.message
          : (outcome.errors[0]?.message ?? 'That did not work.'),
      )
    },
    [],
  )

  const handleAdd = useCallback(
    (target: FavoriteTarget) => {
      void actions.addFavorite(target).then(report)
    },
    [actions, report],
  )

  const handleRemove = useCallback(
    (id: string) => {
      void actions.removeFavorite(id).then(report)
    },
    [actions, report],
  )

  const handleMove = useCallback(
    (id: string, direction: 'up' | 'down') => {
      const plan = reorderPlan(favorites, id, direction)

      // The top row moving up, or the bottom moving down. Nothing to send.
      if (plan.length === 0) {
        return
      }

      void actions.reorderFavorites(plan).then(report)
    },
    [actions, favorites, report],
  )

  /** Everything not yet starred, as one menu. */
  const addItems = useMemo<MenuItem[]>(() => {
    const items: MenuItem[] = []

    for (const [index, team] of teams.filter((t) => !starred.has(t.id)).entries()) {
      items.push({
        id: `team-${team.id}`,
        label: `${team.key} · ${team.name}`,
        separatorBefore: index === 0,
        onSelect: () => {
          handleAdd({ kind: 'team', id: team.id })
        },
      })
    }

    for (const [index, project] of projects
      .filter((p) => !starred.has(p.id))
      .entries()) {
      items.push({
        id: `project-${project.id}`,
        label: project.name,
        separatorBefore: index === 0,
        onSelect: () => {
          handleAdd({ kind: 'project', id: project.id })
        },
      })
    }

    for (const [index, view] of savedViews.filter((v) => !starred.has(v.id)).entries()) {
      items.push({
        id: `view-${view.id}`,
        label: view.name,
        separatorBefore: index === 0,
        onSelect: () => {
          handleAdd({ kind: 'savedView', id: view.id })
        },
      })
    }

    return items
  }, [handleAdd, projects, savedViews, starred, teams])

  const isBusy = isLoading || isLoadingContext
  const hasUnresolved = resolved.some((entry) => entry.name === null)

  return (
    <>
      <PageHeader
        title="Favorites"
        description="The teams, projects and views you starred."
        actions={
          <Menu
            align="end"
            items={addItems}
            label="Add a favorite"
            variant="primary"
          >
            Add favorite
          </Menu>
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
            <VisuallyHidden as="div">Loading favorites</VisuallyHidden>
            {Array.from({ length: 4 }, (_unused, index) => (
              <Skeleton key={index} width="100%" height="2.25rem" />
            ))}
          </div>
        )}

        {!isBusy && errorMessage !== null && (
          <ErrorState
            title="Could not load your favorites"
            description={errorMessage}
            onRetry={retry}
          />
        )}

        {!isBusy && errorMessage === null && resolved.length === 0 && (
          <EmptyState
            icon={<StarIcon />}
            title="Nothing starred yet"
            description="Star a team, a project or a saved view to keep it within reach. Your favorites are yours alone — nobody else sees this list."
          />
        )}

        {!isBusy && errorMessage === null && resolved.length > 0 && (
          <>
            <List label="Your favorites">
              {resolved.map((entry, index) => {
                const { favorite, target, name, kindLabel } = entry

                /*
                  Where a favorite leads. A team is addressed by its key
                  rather than its id, so an unresolved team has no URL at all
                  -- another reason a missing target is a real state and not
                  an edge case to render past.

                  A saved view leads to the saved-views screen and not to the
                  view itself: `ROUTE_SEGMENTS` declares `saved-views` and no
                  `saved-views/:id`, so there is no address for one view. The
                  link is honest about where it goes.
                */
                const team =
                  target?.kind === 'team' ? lookups.teamById.get(target.id) : undefined

                const href =
                  target === null || name === null
                    ? null
                    : target.kind === 'team'
                      ? team === undefined
                        ? null
                        : paths.team(team.key)
                      : target.kind === 'project'
                        ? paths.project(target.id)
                        : paths.savedViews()

                return (
                  <ListRow interactive={href !== null} key={favorite.id}>
                    <ListRowMain>
                      {href === null ? (
                        <span className={styles.missingName}>
                          {name ?? 'Not available'}
                        </span>
                      ) : (
                        <Link className={styles.rowLink} to={href}>
                          {name}
                        </Link>
                      )}
                    </ListRowMain>

                    <ListRowMeta>
                      <Badge tone="neutral">{kindLabel}</Badge>

                      <span className={styles.rowActions}>
                        <IconButton
                          aria-label={`Move ${name ?? kindLabel} up`}
                          disabled={index === 0 || actions.isSaving}
                          icon={<ChevronUpIcon />}
                          onClick={() => {
                            handleMove(favorite.id, 'up')
                          }}
                          size="sm"
                        />
                        <IconButton
                          aria-label={`Move ${name ?? kindLabel} down`}
                          disabled={index === resolved.length - 1 || actions.isSaving}
                          icon={<ChevronDownIcon />}
                          onClick={() => {
                            handleMove(favorite.id, 'down')
                          }}
                          size="sm"
                        />
                        <Menu
                          align="end"
                          items={[
                            {
                              id: 'remove',
                              label: 'Remove from favorites',
                              destructive: true,
                              disabled: actions.isSaving,
                              onSelect: () => {
                                handleRemove(favorite.id)
                              },
                            },
                          ]}
                          label={`Actions on ${name ?? kindLabel}`}
                          size="sm"
                        />
                      </span>
                    </ListRowMeta>
                  </ListRow>
                )
              })}
            </List>

            {hasUnresolved && (
              <p className={styles.footnote}>
                A favorite shown as “Not available” points at something this
                screen could not find — usually because it was deleted after
                being starred. Removing the shortcut clears it.
              </p>
            )}
          </>
        )}
      </PageContent>
    </>
  )
}
