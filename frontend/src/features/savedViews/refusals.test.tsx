import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { renderApp } from '../../test/render'
import { WORKSPACE_SLUG, workspaceContextData } from '../../test/factories'

/**
 * A refused create, end to end, through the real router and cache.
 *
 * `src/features/formRefusals.test.tsx` proves the seven form components no
 * longer drop an error naming a field they draw no control for. This proves
 * the wiring around one of them: that the message survives the mutation hook,
 * the page's outcome handler and the dialog, and reaches the screen.
 *
 * `workspaceSlug` / `NOT_MEMBER` is the real refusal, not an invented one --
 * `app/services/saved_views.py` maps `saved_views_creator_fk` to it, which is
 * what `savedViewCreate` answers when the caller was removed from the
 * workspace while the tab was open. It is also the case a person is most
 * likely to meet and least able to diagnose: the form simply stopped working.
 *
 * The two negative assertions matter as much as the positive one. A dialog
 * that closed, or a list that gained a row, would be a *false success* -- the
 * user walks away believing the view was saved.
 */
const VIEWS_PATH = `/${WORKSPACE_SLUG}/saved-views`

async function openEmptyViews() {
  const app = renderApp({ initialPath: VIEWS_PATH })

  await app.link.resolve('IssueWorkspaceContext', { data: workspaceContextData() })
  await app.link.resolve('SavedViewList', {
    data: {
      savedViews: {
        __typename: 'SavedViewConnection',
        nodes: [],
        pageInfo: { __typename: 'PageInfo', hasNextPage: false, endCursor: null },
      },
    },
  })

  return app
}

describe('a saved view the server refuses', () => {
  it('says so, rather than failing silently with the dialog still open', async () => {
    const app = await openEmptyViews()

    await app.user.click(screen.getByRole('button', { name: 'Create the first view' }))
    await app.user.type(await screen.findByLabelText('Name'), 'Urgent work')
    await app.user.click(screen.getByRole('button', { name: 'Create view' }))

    await app.link.resolve('SavedViewCreate', {
      data: {
        savedViewCreate: {
          __typename: 'SavedViewPayload',
          savedView: null,
          errors: [
            {
              __typename: 'ValidationErrorType',
              field: 'workspaceSlug',
              code: 'NOT_MEMBER',
              message: 'You are no longer a member of this workspace',
            },
          ],
        },
      },
    })

    expect(screen.getByRole('alert')).toHaveTextContent(
      'You are no longer a member of this workspace',
    )

    // Still open, with the typed name still in it: nothing was saved, so
    // nothing about the screen may suggest otherwise.
    expect(screen.getByLabelText('Name')).toHaveValue('Urgent work')

    // And the list did not gain a row it has no server answer for.
    expect(screen.queryByRole('link', { name: /Urgent work/ })).not.toBeInTheDocument()
  })

  it('does not send a second create for a double click', async () => {
    // Two `savedViewCreate`s is two saved views, and the second one is not
    // something anybody asked for. The submit button is the guard.
    const app = await openEmptyViews()

    await app.user.click(screen.getByRole('button', { name: 'Create the first view' }))
    await app.user.type(await screen.findByLabelText('Name'), 'Urgent work')
    await app.user.dblClick(screen.getByRole('button', { name: 'Create view' }))
    await app.link.idle()

    expect(app.link.countOf('SavedViewCreate')).toBe(1)
  })

  it('does not send a second create for Enter pressed twice', async () => {
    // The other way to submit twice, and it does not go through the button's
    // click handler at all in every browser -- so it is asserted separately.
    const app = await openEmptyViews()

    await app.user.click(screen.getByRole('button', { name: 'Create the first view' }))
    await app.user.type(await screen.findByLabelText('Name'), 'Urgent work{Enter}{Enter}')
    await app.link.idle()

    expect(app.link.countOf('SavedViewCreate')).toBe(1)
  })
})
