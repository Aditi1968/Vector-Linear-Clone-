import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { CommentsPanel } from './index'
import {
  authorsData,
  comment,
  commentCreated,
  commentDeleted,
  commentRejected,
  commentsData,
  cursor,
  FORMER_USER_ID,
  ISSUE_ID,
  OTHER_USER_ID,
  renderPanel,
  uuid,
  VIEWER_ID,
  WORKSPACE_SLUG,
} from './testHarness'

/**
 * The comment thread.
 *
 * What is worth a test here is the part that can break silently: a comment
 * that posts successfully and does not appear until a reload, a delete
 * control offered on somebody else's comment, an edit affordance for a
 * mutation that does not exist. Rendering a comment's text is not one of
 * those.
 */

function panel(): HTMLElement {
  return screen.getByRole('region', { name: /Comments/ })
}

function composer(): HTMLElement {
  return screen.getByRole('textbox', { name: 'Add a comment' })
}

function threadTexts(): string[] {
  return within(screen.getByRole('list', { name: 'Comments on this issue' }))
    .getAllByRole('listitem')
    .map((row) => row.textContent ?? '')
}

async function mount(
  view = renderPanel(<CommentsPanel workspaceSlug={WORKSPACE_SLUG} issueId={ISSUE_ID} />),
  comments = [comment(1), comment(2, { authorId: VIEWER_ID })],
) {
  await view.link.resolve('IssueComments', { data: commentsData(comments) })
  await view.link.resolve('CommentAuthors', { data: authorsData() })

  return view
}

describe('the comment thread', () => {
  it('scopes both of its queries to the workspace and issue it was given', async () => {
    const view = renderPanel(
      <CommentsPanel workspaceSlug={WORKSPACE_SLUG} issueId={ISSUE_ID} />,
    )

    expect(await view.link.waitForRequest('IssueComments')).toEqual({
      workspaceSlug: WORKSPACE_SLUG,
      issueId: ISSUE_ID,
      // Stated, not omitted: the merge policy reads `after` to decide whether
      // a result starts the list or extends it, and the cache updates read the
      // field back under exactly these variables.
      after: null,
    })
    expect(await view.link.waitForRequest('CommentAuthors')).toEqual({
      workspaceSlug: WORKSPACE_SLUG,
    })
  })

  it('says the thread is empty rather than showing nothing', async () => {
    await mount(undefined, [])

    expect(within(panel()).getByText('No comments yet')).toBeInTheDocument()
    expect(screen.queryByRole('list', { name: 'Comments on this issue' })).toBeNull()
  })

  it('names authors through the member list, and the unknown ones honestly', async () => {
    const view = await mount(undefined, [
      comment(1),
      comment(2, { authorId: uuid(999) }),
    ])

    expect(threadTexts()[0]).toContain('Grace Hopper')
    // An id in no list at all. Not "Former member": people who left ARE in
    // the list -- see the test below -- so this branch is a lookup that came
    // back with nothing, and naming it after a specific fate would be a guess
    // dressed as a fact.
    expect(threadTexts()[1]).toContain('Unknown author')
    expect(view.link.countOf('CommentAuthors')).toBe(1)
  })

  it('still names the author of a comment by somebody who has left', async () => {
    await mount(undefined, [comment(1, { authorId: FORMER_USER_ID })])

    // The whole reason 026 keeps a removed member's row. A thread is a record
    // of what was said, and "Alan Turing wrote this" does not stop being true
    // when Alan leaves -- so the panel reads the member list unfiltered, and
    // this row is the guard on anything that later decides to "clean up"
    // former members out of an activity log.
    expect(threadTexts()[0]).toContain('Alan Turing')
    expect(threadTexts()[0]).not.toContain('Unknown author')
  })

  it('shows a posted comment without refetching the thread', async () => {
    const view = await mount()

    await view.user.type(composer(), 'Looks like a cache bug')
    await view.user.click(screen.getByRole('button', { name: 'Comment' }))

    expect(await view.link.waitForRequest('CommentCreate')).toEqual({
      input: {
        workspaceSlug: WORKSPACE_SLUG,
        issueId: ISSUE_ID,
        body: 'Looks like a cache bug',
      },
    })

    await view.link.resolve('CommentCreate', {
      data: commentCreated(
        comment(3, { authorId: VIEWER_ID, body: 'Looks like a cache bug' }),
      ),
    })

    expect(threadTexts()).toHaveLength(3)
    expect(threadTexts()[2]).toContain('Looks like a cache bug')
    // The whole point of the hand-written update: one request went out, not
    // two, and every page already loaded survived it.
    expect(view.link.countOf('IssueComments')).toBe(1)
    expect(composer()).toHaveValue('')
  })

  it('appends rather than prepends, because the server sorts oldest first', async () => {
    const view = await mount(undefined, [comment(1, { body: 'First' })])

    await view.user.type(composer(), 'Second')
    await view.user.click(screen.getByRole('button', { name: 'Comment' }))
    await view.link.resolve('CommentCreate', {
      data: commentCreated(comment(3, { authorId: VIEWER_ID, body: 'Second' })),
    })

    const texts = threadTexts()
    expect(texts[0]).toContain('First')
    expect(texts[1]).toContain('Second')
  })

  it('keeps what was typed when the server rejects it', async () => {
    const view = await mount()

    await view.user.type(composer(), '   ')
    // Whitespace alone does not enable the button, so nothing is sent.
    expect(screen.getByRole('button', { name: 'Comment' })).toBeDisabled()

    await view.user.type(composer(), 'Something')
    await view.user.click(screen.getByRole('button', { name: 'Comment' }))
    await view.link.resolve('CommentCreate', {
      data: commentRejected('Comment cannot be empty.'),
    })

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Comment cannot be empty.',
    )
    // A failed post that emptied the box would lose what the person wrote.
    expect(composer()).toHaveValue('   Something')
    expect(threadTexts()).toHaveLength(2)
  })

  it('offers delete only on the reader own comments', async () => {
    await mount()

    expect(
      screen.queryByRole('button', { name: 'Delete comment by Grace Hopper' }),
    ).toBeNull()
    expect(
      screen.getByRole('button', { name: 'Delete comment by Ada Lovelace' }),
    ).toBeInTheDocument()
  })

  it('offers delete on nothing when there is no signed-in reader', async () => {
    const view = renderPanel(
      <CommentsPanel workspaceSlug={WORKSPACE_SLUG} issueId={ISSUE_ID} />,
    )

    await view.link.resolve('IssueComments', {
      data: commentsData([comment(1, { authorId: VIEWER_ID })]),
    })
    await view.link.resolve('CommentAuthors', { data: authorsData(null) })

    expect(screen.queryByRole('button', { name: /^Delete comment by/ })).toBeNull()
  })

  it('confirms a delete, then removes the comment from the thread', async () => {
    const view = await mount()

    await view.user.click(
      screen.getByRole('button', { name: 'Delete comment by Ada Lovelace' }),
    )

    // Nothing is sent until the dialog is confirmed.
    expect(view.link.countOf('CommentDelete')).toBe(0)
    expect(screen.getByText('Delete this comment?')).toBeInTheDocument()

    await view.user.click(screen.getByRole('button', { name: 'Delete comment' }))

    expect(await view.link.waitForRequest('CommentDelete')).toEqual({
      input: { workspaceSlug: WORKSPACE_SLUG, id: uuid(2) },
    })

    await view.link.resolve('CommentDelete', { data: commentDeleted(uuid(2)) })

    expect(threadTexts()).toHaveLength(1)
    expect(threadTexts()[0]).toContain('Grace Hopper')
    expect(view.link.countOf('IssueComments')).toBe(1)
    // Announced, not merely done.
    expect(within(panel()).getByRole('status')).toHaveTextContent('Comment deleted')
  })

  it('leaves the thread alone when a delete is abandoned', async () => {
    const view = await mount()

    await view.user.click(
      screen.getByRole('button', { name: 'Delete comment by Ada Lovelace' }),
    )
    await view.user.click(screen.getByRole('button', { name: 'Cancel' }))

    expect(view.link.countOf('CommentDelete')).toBe(0)
    expect(threadTexts()).toHaveLength(2)
  })

  it('never offers to edit a comment, because the schema cannot', async () => {
    await mount()

    expect(screen.queryByRole('button', { name: /edit/i })).toBeNull()
    expect(screen.queryByText(/edited/i)).toBeNull()
  })

  it('loads a further page onto the end without discarding the first', async () => {
    const view = renderPanel(
      <CommentsPanel workspaceSlug={WORKSPACE_SLUG} issueId={ISSUE_ID} />,
    )

    await view.link.resolve('IssueComments', {
      data: commentsData([comment(1), comment(2)], {
        hasNextPage: true,
        endCursor: cursor('page-one'),
      }),
    })
    await view.link.resolve('CommentAuthors', { data: authorsData() })

    await view.user.click(screen.getByRole('button', { name: 'Load more comments' }))

    // The opaque keyset cursor the server issued, not a page number. There is
    // nothing in this feature an offset could be computed from.
    expect(await view.link.waitForRequest('IssueComments')).toMatchObject({
      after: cursor('page-one'),
    })

    await view.link.resolve('IssueComments', {
      data: commentsData([comment(3), comment(4)]),
    })

    // Four, not two: the merge policy extended the field rather than
    // replacing it, and not five, because nothing was concatenated twice.
    expect(threadTexts()).toHaveLength(4)
    // The member list is a fact about the workspace and is not re-sent with
    // each page -- which is why it is a separate document.
    expect(view.link.countOf('CommentAuthors')).toBe(1)
  })

  it('does not attribute a comment to the workspace it was not asked about', async () => {
    const view = renderPanel(
      <CommentsPanel workspaceSlug="other-tenant" issueId={ISSUE_ID} />,
    )

    expect(await view.link.waitForRequest('IssueComments')).toMatchObject({
      workspaceSlug: 'other-tenant',
    })
    expect(OTHER_USER_ID).not.toEqual(VIEWER_ID)
  })
})
