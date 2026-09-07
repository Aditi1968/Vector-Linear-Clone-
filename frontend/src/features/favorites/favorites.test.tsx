import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { main, renderApp } from '../../test/render'
import {
  PROJECT_ID,
  TEAM_ID,
  WORKSPACE_SLUG,
  workspaceContextData,
} from '../../test/factories'
import type { FavoriteListQuery, FavoriteSavedViewsQuery } from '../../generated/operations'

/**
 * The favorites screen, against the real router, cache and a controlled
 * network.
 *
 * Two claims carry the weight here. A `Favorite` has no name, so every label
 * is a join against lists the screen already holds -- and a target that is not
 * in them must read as "not available" rather than as a blank row or a name
 * the screen invented. And reordering has to survive positions that are
 * neither unique nor contiguous, which is what the schema permits.
 */

const FAV_TEAM = '00000000-0000-4000-8000-0000000e0001'
const FAV_PROJECT = '00000000-0000-4000-8000-0000000e0002'
const FAV_MISSING = '00000000-0000-4000-8000-0000000e0003'
const VIEW_ID = '00000000-0000-4000-8000-0000000f0001'
const GONE_ID = '00000000-0000-4000-8000-0000000f0009'

const FAVORITES_PATH = `/${WORKSPACE_SLUG}/favorites`

type FavoriteRow = FavoriteListQuery['favorites'][number]

function teamFavorite(position = 0, id = FAV_TEAM): FavoriteRow {
  return {
    __typename: 'Favorite',
    id,
    teamId: TEAM_ID,
    projectId: null,
    savedViewId: null,
    position,
  }
}

function projectFavorite(position = 1, id = FAV_PROJECT): FavoriteRow {
  return {
    __typename: 'Favorite',
    id,
    teamId: null,
    projectId: PROJECT_ID,
    savedViewId: null,
    position,
  }
}

const savedViewsData: FavoriteSavedViewsQuery = {
  savedViews: {
    __typename: 'SavedViewConnection',
    nodes: [{ __typename: 'SavedView', id: VIEW_ID, name: 'My open bugs' }],
  },
}

async function openFavorites(favorites: readonly FavoriteRow[]) {
  const app = renderApp({ initialPath: FAVORITES_PATH })

  await app.link.resolve('IssueWorkspaceContext', { data: workspaceContextData() })
  await app.link.resolve('FavoriteSavedViews', { data: savedViewsData })
  await app.link.resolve('FavoriteList', { data: { favorites: [...favorites] } })

  return app
}

describe('the favorites list', () => {
  it('resolves a name for each target rather than showing raw ids', async () => {
    await openFavorites([teamFavorite(), projectFavorite()])

    const list = within(main()).getByRole('list', { name: 'Your favorites' })

    /*
      `Favorite` carries `{ id, teamId, projectId, savedViewId, position }`
      and no name at all -- there is no `Favorite.team` in the schema. These
      two labels are a join against the workspace context, done once for the
      screen rather than once per row.
    */
    expect(within(list).getByRole('link', { name: 'Engineering' })).toBeInTheDocument()
    expect(within(list).getByRole('link', { name: 'Platform' })).toBeInTheDocument()
  })

  it('says a target is not available instead of inventing a name for it', async () => {
    await openFavorites([
      {
        __typename: 'Favorite',
        id: FAV_MISSING,
        teamId: null,
        projectId: null,
        savedViewId: GONE_ID,
        position: 0,
      },
    ])

    /*
      A favorite can outlive what it points at. That is an ordinary outcome,
      not an error -- and the honest rendering is to say the target could not
      be found, never to guess a name or to drop the row so that a shortcut
      someone remembers adding silently disappears.
    */
    expect(within(main()).getByText('Not available')).toBeInTheDocument()
    expect(within(main()).getByText(/points at something this screen could not find/i))
      .toBeInTheDocument()
  })

  it('orders by position with an id tiebreak, as the server does', async () => {
    await openFavorites([
      // Deliberately out of order, and sharing a position: the schema says
      // `position` is neither unique nor contiguous and ties break by id.
      projectFavorite(0, FAV_PROJECT),
      teamFavorite(0, FAV_TEAM),
    ])

    const links = within(main())
      .getByRole('list', { name: 'Your favorites' })
      .querySelectorAll('a')

    // FAV_TEAM (...e0001) sorts before FAV_PROJECT (...e0002) on the tiebreak.
    expect(links[0]).toHaveTextContent('Engineering')
    expect(links[1]).toHaveTextContent('Platform')
  })

  it('says nothing is starred rather than showing an empty list', async () => {
    await openFavorites([])

    expect(within(main()).getByText('Nothing starred yet')).toBeInTheDocument()
  })

  it('offers a retry when the list could not be loaded', async () => {
    const app = renderApp({ initialPath: FAVORITES_PATH })

    await app.link.resolve('IssueWorkspaceContext', { data: workspaceContextData() })
    await app.link.resolve('FavoriteSavedViews', { data: savedViewsData })
    await app.link.fail('FavoriteList', new Error('Network unreachable'))

    const alert = await screen.findByRole('alert')

    expect(alert).toHaveTextContent('Could not load your favorites')

    await app.user.click(within(alert).getByRole('button', { name: 'Try again' }))

    await expect(app.link.waitForRequest('FavoriteList')).resolves.toEqual({
      workspaceSlug: WORKSPACE_SLUG,
    })
  })
})

describe('changing the favorites list', () => {
  it('adds exactly one target column and nulls the other two', async () => {
    const app = await openFavorites([])

    await app.user.click(screen.getByRole('button', { name: 'Add favorite' }))
    await app.user.click(screen.getByRole('menuitem', { name: 'ENG · Engineering' }))

    /*
      `FavoriteAddInput` requires exactly one of the three and refuses none or
      more than one rather than guessing which was meant. Expanding a tagged
      union in the hook is what makes an invalid combination unrepresentable
      at the call site.
    */
    await expect(app.link.waitForRequest('FavoriteAdd')).resolves.toEqual({
      input: {
        workspaceSlug: WORKSPACE_SLUG,
        teamId: TEAM_ID,
        projectId: null,
        savedViewId: null,
      },
    })
  })

  it('does not offer to star something already starred', async () => {
    const app = await openFavorites([teamFavorite()])

    await app.user.click(screen.getByRole('button', { name: 'Add favorite' }))

    expect(screen.queryByRole('menuitem', { name: 'ENG · Engineering' })).toBeNull()
    expect(screen.getByRole('menuitem', { name: 'Platform' })).toBeInTheDocument()
  })

  it('removes a favorite and reloads the list', async () => {
    const app = await openFavorites([teamFavorite()])

    await app.user.click(screen.getByRole('button', { name: 'Actions on Engineering' }))
    await app.user.click(screen.getByRole('menuitem', { name: 'Remove from favorites' }))

    await expect(app.link.waitForRequest('FavoriteRemove')).resolves.toEqual({
      input: { workspaceSlug: WORKSPACE_SLUG, id: FAV_TEAM },
    })

    await app.link.resolve('FavoriteRemove', {
      data: {
        favoriteRemove: {
          __typename: 'FavoriteDeletePayload',
          deletedFavoriteId: FAV_TEAM,
          errors: [],
        },
      },
    })

    // `favorites` is a plain list field this payload does not contain, so
    // nothing normalisation does can take the row out of it.
    expect(app.link.countOf('FavoriteList')).toBe(2)
  })

  it('cannot move the first row up or the last row down', async () => {
    await openFavorites([teamFavorite(0), projectFavorite(1)])

    expect(screen.getByRole('button', { name: 'Move Engineering up' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Move Platform down' })).toBeDisabled()
  })

  it('renumbers a move rather than trusting positions to be distinct', async () => {
    const app = await openFavorites([
      // Both at position 0, which the schema permits. A naive swap of two
      // equal positions changes nothing and leaves the button looking broken.
      teamFavorite(0, FAV_TEAM),
      projectFavorite(0, FAV_PROJECT),
    ])

    await app.user.click(screen.getByRole('button', { name: 'Move Platform up' }))

    /*
      The row that moves in the *request* is the other one. Both sat at
      position 0, so putting Platform above Engineering is achieved by giving
      Engineering position 1 -- Platform's own position is already correct and
      is not re-sent.

      That is the whole point of planning against the desired order rather
      than swapping two values: a swap of two equal positions writes the same
      numbers back and changes nothing, and the button would look broken on
      exactly the list the schema says is legal.
    */
    await expect(app.link.waitForRequest('FavoriteReorder')).resolves.toEqual({
      input: { workspaceSlug: WORKSPACE_SLUG, id: FAV_TEAM, position: 1 },
    })
  })

  it('reports a refusal instead of leaving the list looking reordered', async () => {
    const app = await openFavorites([teamFavorite(0), projectFavorite(1)])

    await app.user.click(screen.getByRole('button', { name: 'Move Platform up' }))

    await app.link.resolve('FavoriteReorder', {
      data: {
        favoriteReorder: {
          __typename: 'FavoritePayload',
          favorite: null,
          errors: [
            {
              __typename: 'ValidationErrorType',
              field: 'id',
              code: 'NOT_FOUND',
              message: 'That favorite is gone.',
            },
          ],
        },
      },
    })

    expect(await screen.findByRole('alert')).toHaveTextContent('That favorite is gone.')
  })
})
