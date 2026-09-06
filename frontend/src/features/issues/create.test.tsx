import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import {
  cursor,
  issueCreated,
  issueDetail,
  issueListData,
  issueRejected,
  issueRow,
  validationError,
} from '../../test/factories'
import { issueRowTexts, main, renderApp } from '../../test/render'

/**
 * Creating an issue.
 *
 * The two failure channels get equal weight here, because the backend uses
 * both and they mean different things. `IssueCreatePayload.errors` arrives
 * *inside `data`* over a successful response and describes user input; a
 * rejected promise is an outage or a bug. A client that reads only one either
 * discards validation feedback or reports a database failure as a bad title,
 * and only one of those is visible without a test.
 */

const PAGE_ONE_END = cursor('page-one')

/**
 * The composer form.
 *
 * Reached through the title field rather than by `getByRole('form')`: an
 * unnamed `<form>` is a generic element and not a `form` landmark, and this
 * one is named only by a visible `<h2>` that it does not point at with
 * `aria-labelledby`. That is a defensible choice -- an inline composer is not
 * obliged to be a landmark -- so the test works with the DOM as it is instead
 * of asserting a role the component never claimed.
 */
function composer(): HTMLElement {
  const form = titleField().closest('form')

  if (form === null) {
    throw new Error('The title field is not inside a form')
  }

  return form
}

function titleField(): HTMLElement {
  return screen.getByRole('textbox', { name: 'Title' })
}

async function openComposer(view: ReturnType<typeof renderApp>): Promise<void> {
  await view.user.click(screen.getByRole('button', { name: 'New issue' }))
}

describe('creating an issue', () => {
  it('opens the composer from the shell button', async () => {
    const view = renderApp()

    await view.link.resolve('IssueList', {
      data: issueListData([issueRow(1, { title: 'Alpha' })]),
    })

    expect(screen.queryByRole('heading', { name: 'New issue' })).toBeNull()

    await openComposer(view)

    expect(screen.getByRole('heading', { name: 'New issue' })).toBeInTheDocument()
    expect(titleField()).toBeInTheDocument()
  })

  it('opens the composer from the empty state', async () => {
    const { link, user } = renderApp()

    await link.resolve('IssueList', { data: issueListData([]) })

    await user.click(screen.getByRole('button', { name: 'Create the first issue' }))

    expect(screen.getByRole('heading', { name: 'New issue' })).toBeInTheDocument()
  })

  it('accepts a typed title and reports its length', async () => {
    const view = renderApp()

    await view.link.resolve('IssueList', { data: issueListData([]) })
    await openComposer(view)

    await view.user.type(titleField(), 'Login redirect loops')

    expect(titleField()).toHaveValue('Login redirect loops')
    // The counter is part of the field's accessible description, so it is
    // announced rather than only drawn.
    expect(titleField()).toHaveAccessibleDescription(/20 \/ 500/)
  })

  it('sends exactly the fields the mutation takes', async () => {
    const view = renderApp()

    await view.link.resolve('IssueList', { data: issueListData([]) })
    await openComposer(view)

    await view.user.type(titleField(), 'Login redirect loops')
    await view.user.type(
      screen.getByRole('textbox', { name: /Description/ }),
      'Only after a password reset.',
    )
    await view.user.selectOptions(screen.getByRole('combobox', { name: 'Priority' }), '2')

    await view.user.click(screen.getByRole('button', { name: 'Create issue' }))

    const variables = await view.link.waitForRequest('IssueCreate')

    // `IssueCreateInput` is `{ title, description, priority }` and nothing
    // else -- there is no team, project, assignee or status to send.
    expect(variables).toEqual({
      input: {
        title: 'Login redirect loops',
        description: 'Only after a password reset.',
        priority: 2,
      },
    })
  })

  it('sends a null description rather than an empty string', async () => {
    const view = renderApp()

    await view.link.resolve('IssueList', { data: issueListData([]) })
    await openComposer(view)

    await view.user.type(titleField(), 'No body')
    await view.user.click(screen.getByRole('button', { name: 'Create issue' }))

    const variables = await view.link.waitForRequest('IssueCreate')

    expect(variables).toEqual({
      input: { title: 'No body', description: null, priority: 0 },
    })
  })

  it('sends the title exactly as typed, without trimming', async () => {
    const view = renderApp()

    await view.link.resolve('IssueList', { data: issueListData([]) })
    await openComposer(view)

    await view.user.type(titleField(), '  padded  ')
    await view.user.click(screen.getByRole('button', { name: 'Create issue' }))

    const variables = await view.link.waitForRequest('IssueCreate')

    /*
      The service validates the title "as supplied -- never trimmed or
      rewritten". A client that trimmed would make the two disagree about
      what was submitted: a title of three spaces would come back rejected as
      REQUIRED for a value still visible in the box.
    */
    expect(variables).toEqual({
      input: { title: '  padded  ', description: null, priority: 0 },
    })
  })

  it('shows the new issue without reloading or refetching the list', async () => {
    const view = renderApp()

    await view.link.resolve('IssueList', {
      data: issueListData([issueRow(1, { title: 'Alpha' })], {
        hasNextPage: true,
        endCursor: PAGE_ONE_END,
      }),
    })

    // A second page, so the test can tell a cache update from a refetch:
    // refetching page one would discard this row.
    await view.user.click(within(main()).getByRole('button', { name: 'Load more' }))
    await view.link.resolve('IssueList', {
      data: issueListData([issueRow(2, { title: 'Bravo' })], {
        hasNextPage: false,
        endCursor: cursor('page-two'),
      }),
    })

    expect(view.link.countOf('IssueList')).toBe(2)

    const before = issueRowTexts().length

    await openComposer(view)
    await view.user.type(titleField(), 'Charlie')
    await view.user.click(screen.getByRole('button', { name: 'Create issue' }))

    await view.link.resolve('IssueCreate', {
      data: issueCreated(issueDetail(9, { title: 'Charlie' })),
    })

    const rows = issueRowTexts()

    // Exactly one more row, at the head -- which is where the server's
    // `created_at DESC` ordering would put it.
    expect(rows).toHaveLength(before + 1)
    expect(new Set(rows).size).toBe(rows.length)
    expect(rows).toHaveLength(3)
    expect(rows[0]).toContain('Charlie')
    expect(rows[1]).toContain('Alpha')
    expect(rows[2]).toContain('Bravo')

    // No third list request: the row arrived by a cache write, not a reload.
    // A refetch would also have sent `after: null` and collapsed the two
    // pages back to one.
    expect(view.link.countOf('IssueList')).toBe(2)

    // The composer closes, revealing the row in place.
    expect(screen.queryByRole('heading', { name: 'New issue' })).toBeNull()
  })

  it('surfaces a validation error returned inside the payload', async () => {
    const view = renderApp()

    // A populated list, so the test can also show that a rejected create
    // leaves the rows alone.
    await view.link.resolve('IssueList', {
      data: issueListData([
        issueRow(1, { title: 'Alpha' }),
        issueRow(2, { title: 'Bravo' }),
      ]),
    })
    await openComposer(view)

    await view.user.type(titleField(), 'x')
    await view.user.click(screen.getByRole('button', { name: 'Create issue' }))

    // A successful response -- no GraphQL `errors` array -- carrying a
    // rejection inside `data`.
    await view.link.resolve('IssueCreate', {
      data: issueRejected(
        validationError('title', 'TOO_LONG', 'Title must be at most 500 characters.'),
      ),
    })

    expect(titleField()).toBeInvalid()
    expect(titleField()).toHaveAccessibleDescription(
      /Title must be at most 500 characters\./,
    )

    // Focus moves to the field the server rejected; otherwise a keyboard
    // submit leaves focus on the button and the new message is never
    // announced.
    expect(titleField()).toHaveFocus()

    // The composer stays open with the user's text intact.
    expect(composer()).toBeInTheDocument()
    expect(titleField()).toHaveValue('x')

    // And the list is exactly as it was. There is no issue to add, and
    // `errors` is the form's business rather than the cache's.
    const rows = issueRowTexts()
    expect(rows).toHaveLength(2)
    expect(rows[0]).toContain('Alpha')
    expect(rows[1]).toContain('Bravo')
    expect(view.link.countOf('IssueList')).toBe(1)
  })

  it('surfaces every validation error the server returns at once', async () => {
    const view = renderApp()

    await view.link.resolve('IssueList', { data: issueListData([]) })
    await openComposer(view)

    await view.user.click(screen.getByRole('button', { name: 'Create issue' }))

    await view.link.resolve('IssueCreate', {
      data: issueRejected(
        validationError('title', 'REQUIRED', 'Title is required.'),
        validationError('priority', 'OUT_OF_RANGE', 'Priority must be between 0 and 4.'),
      ),
    })

    // The service collects every violation before raising, so the form must
    // render all of them rather than only the first.
    expect(titleField()).toHaveAccessibleDescription(/Title is required\./)
    expect(screen.getByRole('combobox', { name: 'Priority' })).toHaveAccessibleDescription(
      /Priority must be between 0 and 4\./,
    )
  })

  it('surfaces a transport failure at the top of the form', async () => {
    const view = renderApp()

    await view.link.resolve('IssueList', { data: issueListData([]) })
    await openComposer(view)

    await view.user.type(titleField(), 'Alpha')
    await view.user.click(screen.getByRole('button', { name: 'Create issue' }))

    await view.link.fail('IssueCreate', new Error('Failed to fetch'))

    const alert = within(composer()).getByRole('alert')
    expect(alert).toHaveTextContent('Failed to fetch')

    // No field owns a transport failure, so no field is marked invalid.
    expect(titleField()).not.toBeInvalid()
    expect(composer()).toBeInTheDocument()
  })

  it('closes the composer on Cancel without sending anything', async () => {
    const view = renderApp()

    await view.link.resolve('IssueList', { data: issueListData([]) })
    await openComposer(view)

    await view.user.type(titleField(), 'Abandoned')
    await view.user.click(screen.getByRole('button', { name: 'Cancel' }))

    expect(screen.queryByRole('heading', { name: 'New issue' })).toBeNull()
    expect(view.link.countOf('IssueCreate')).toBe(0)
  })
})
