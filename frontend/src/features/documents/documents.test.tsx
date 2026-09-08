import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import {
  expectEveryButtonNamed,
  expectHeadingLevelsUnbroken,
  expectNoDuplicateButtonNames,
  expectOneFirstLevelHeading,
} from '../../test/a11y'
import { main, renderApp } from '../../test/render'
import { MEMBER_ID, WORKSPACE_SLUG, workspaceContextData } from '../../test/factories'
import type { DocumentDetailFieldsFragment } from '../../generated/operations'
import { fromPlainText, isPlainTextDocument, readDocument, safeHref } from './lib/content'

/**
 * The documents screen, against the real router, cache and a controlled
 * network.
 *
 * ## The claim that matters most
 *
 * A document's body is a ProseMirror tree with headings, lists, quotes, code
 * blocks and five marks. The editor here is a textarea, which can carry
 * paragraphs and line breaks and nothing else -- so a formatted document
 * edited through it would come back stripped. That is the template feature's
 * whole-row-replace bug wearing a different hat: an edit to one part of a
 * document silently deleting another, invisible until somebody looks at what
 * used to be a list.
 *
 * So the editor refuses to open on a document it cannot round-trip, and the
 * test below is what keeps that refusal in place.
 */

const DOCUMENTS_PATH = `/${WORKSPACE_SLUG}/documents`

const DOCUMENT_ID = '00000000-0000-4000-8000-0000000ff001'
const REVISION_ID = '00000000-0000-4000-8000-0000000ff0a1'

const PLAIN_BODY = {
  type: 'doc',
  content: [{ type: 'paragraph', content: [{ type: 'text', text: 'The first line.' }] }],
}

const FORMATTED_BODY = {
  type: 'doc',
  content: [
    { type: 'heading', attrs: { level: 1 }, content: [{ type: 'text', text: 'Scope' }] },
    {
      type: 'bulletList',
      content: [
        {
          type: 'listItem',
          content: [{ type: 'paragraph', content: [{ type: 'text', text: 'One' }] }],
        },
      ],
    },
  ],
}

function documentDetail(
  overrides: Partial<DocumentDetailFieldsFragment> = {},
): DocumentDetailFieldsFragment {
  return {
    __typename: 'Document',
    id: DOCUMENT_ID,
    title: 'Launch plan',
    projectId: null,
    initiativeId: null,
    creatorId: MEMBER_ID,
    lastEditedBy: MEMBER_ID,
    createdAt: '2026-01-01T12:00:00.000Z',
    updatedAt: '2026-01-15T12:00:00.000Z',
    content: PLAIN_BODY,
    revisions: {
      __typename: 'DocumentRevisionConnection',
      nodes: [],
      pageInfo: { __typename: 'PageInfo', hasNextPage: false, endCursor: null },
    },
    comments: {
      __typename: 'DocumentCommentConnection',
      nodes: [],
      pageInfo: { __typename: 'PageInfo', hasNextPage: false, endCursor: null },
    },
    ...overrides,
  }
}

function listRow(document: DocumentDetailFieldsFragment) {
  return {
    __typename: 'Document' as const,
    id: document.id,
    title: document.title,
    projectId: document.projectId,
    initiativeId: document.initiativeId,
    creatorId: document.creatorId,
    lastEditedBy: document.lastEditedBy,
    createdAt: document.createdAt,
    updatedAt: document.updatedAt,
  }
}

async function openDocuments(
  documents: readonly DocumentDetailFieldsFragment[],
  { hasNextPage = false }: { hasNextPage?: boolean } = {},
) {
  const app = renderApp({ initialPath: DOCUMENTS_PATH })

  await app.link.resolve('IssueWorkspaceContext', { data: workspaceContextData() })

  // The screen reuses the initiatives list to name a document's initiative
  // rather than running a second copy of that query.
  await app.link.resolve('InitiativeList', {
    data: {
      initiatives: {
        __typename: 'InitiativeConnection',
        nodes: [],
        pageInfo: { __typename: 'PageInfo', hasNextPage: false, endCursor: null },
      },
    },
  })

  await app.link.resolve('DocumentList', {
    data: {
      documents: {
        __typename: 'DocumentConnection',
        nodes: documents.map(listRow),
        pageInfo: {
          __typename: 'PageInfo',
          hasNextPage,
          endCursor: hasNextPage ? 'document-cursor' : null,
        },
      },
    },
  })

  if (documents.length > 0) {
    await app.link.resolve('DocumentDetail', { data: { document: documents[0] } })
  }

  return app
}

describe('the document list', () => {
  it('opens the first document beside the list', async () => {
    await openDocuments([documentDetail()])

    expect(
      await screen.findByRole('complementary', { name: 'Document Launch plan' }),
    ).toBeInTheDocument()
    expect(screen.getByText('The first line.')).toBeInTheDocument()
  })

  it('says there are none rather than showing an empty list', async () => {
    await openDocuments([])

    expect(within(main()).getByText('No documents yet')).toBeInTheDocument()
  })

  it('offers a real next page', async () => {
    const app = await openDocuments([documentDetail()], { hasNextPage: true })

    await app.user.click(screen.getByRole('button', { name: 'Load more' }))

    await expect(app.link.waitForRequest('DocumentList')).resolves.toEqual({
      workspaceSlug: WORKSPACE_SLUG,
      after: 'document-cursor',
    })
  })

  it('offers a retry when the list could not be loaded', async () => {
    const app = renderApp({ initialPath: DOCUMENTS_PATH })

    await app.link.resolve('IssueWorkspaceContext', { data: workspaceContextData() })
    await app.link.fail('DocumentList', new Error('Network unreachable'))

    const alert = await screen.findByRole('alert')

    expect(alert).toHaveTextContent('Could not load documents')
  })
})

describe('editing a document', () => {
  it('refuses to open a plain-text editor over a formatted document', async () => {
    await openDocuments([documentDetail({ content: FORMATTED_BODY })])

    // The document is still rendered -- the heading and the list are there.
    expect(await screen.findByText('Scope')).toBeInTheDocument()
    expect(screen.getByText('One')).toBeInTheDocument()

    /*
      And the editor is not offered, because opening a textarea over this and
      saving it back would delete the heading and the list without saying so.
    */
    expect(screen.queryByRole('button', { name: 'Edit body' })).not.toBeInTheDocument()
    expect(
      screen.getByText(/plain-text editor cannot carry/i),
    ).toBeInTheDocument()
  })

  it('round-trips a plain document through the editor', async () => {
    const app = await openDocuments([documentDetail()])

    await app.user.click(await screen.findByRole('button', { name: 'Edit body' }))

    const body = await screen.findByLabelText('Document body')

    expect(body).toHaveValue('The first line.')

    await app.user.clear(body)
    await app.user.type(body, 'Rewritten.')
    await app.user.click(screen.getByRole('button', { name: 'Save body' }))

    /*
      `snapshot: false` is SENT rather than omitted: the input declares it
      non-null with a default, and stating it keeps what the client asked for
      readable on the wire. `title` is absent, which is what makes this a body
      edit and not a rename -- `DocumentEditInput` leaves an absent field
      alone.
    */
    await expect(app.link.waitForRequest('DocumentEdit')).resolves.toEqual({
      input: {
        workspaceSlug: WORKSPACE_SLUG,
        id: DOCUMENT_ID,
        snapshot: false,
        content: {
          type: 'doc',
          content: [
            { type: 'paragraph', content: [{ type: 'text', text: 'Rewritten.' }] },
          ],
        },
      },
    })
  })

  it('renames without sending the body', async () => {
    const app = await openDocuments([documentDetail()])

    const title = await screen.findByLabelText('Document title')

    await app.user.clear(title)
    await app.user.type(title, 'Launch plan v2')
    await app.user.click(screen.getByRole('button', { name: 'Rename' }))

    await expect(app.link.waitForRequest('DocumentEdit')).resolves.toEqual({
      input: {
        workspaceSlug: WORKSPACE_SLUG,
        id: DOCUMENT_ID,
        snapshot: false,
        title: 'Launch plan v2',
      },
    })
  })

  it('lets a version be read before it is restored', async () => {
    const app = await openDocuments([
      documentDetail({
        revisions: {
          __typename: 'DocumentRevisionConnection',
          nodes: [
            {
              __typename: 'DocumentRevision',
              id: REVISION_ID,
              documentId: DOCUMENT_ID,
              title: 'Launch plan',
              authorId: MEMBER_ID,
              createdAt: '2026-01-10T12:00:00.000Z',
              content: {
                type: 'doc',
                content: [
                  { type: 'paragraph', content: [{ type: 'text', text: 'The older line.' }] },
                ],
              },
            },
          ],
          pageInfo: { __typename: 'PageInfo', hasNextPage: false, endCursor: null },
        },
      }),
    ])

    await app.user.click(await screen.findByRole('tab', { name: /History/ }))

    /*
      The version's text is on the page. A picker that showed only "3 days
      ago, by Ada" would be asking somebody to overwrite the current document
      with a version they have not read.
    */
    expect(screen.getByText('The older line.')).toBeInTheDocument()

    await app.user.click(screen.getByRole('button', { name: /^Restore the version from/ }))

    await expect(app.link.waitForRequest('DocumentRestore')).resolves.toEqual({
      input: {
        workspaceSlug: WORKSPACE_SLUG,
        documentId: DOCUMENT_ID,
        revisionId: REVISION_ID,
      },
    })
  })
})

describe('the content reader', () => {
  it('tells an empty document from a value that is not a document', () => {
    // `{"type":"doc","content":[]}` is what the server's `empty_content()`
    // produces: somebody made a document and has not written in it.
    expect(readDocument({ type: 'doc', content: [] })).toEqual([])

    // Null is a value that is not a document tree at all, which the panel
    // reports rather than drawing as a blank page.
    expect(readDocument('not a document')).toBeNull()
    expect(readDocument(null)).toBeNull()
  })

  it('will not round-trip anything a textarea would strip', () => {
    expect(isPlainTextDocument(readDocument(PLAIN_BODY) ?? [])).toBe(true)
    expect(isPlainTextDocument(readDocument(FORMATTED_BODY) ?? [])).toBe(false)

    // A single bold word is enough: the mark would not survive the textarea.
    const marked = readDocument({
      type: 'doc',
      content: [
        {
          type: 'paragraph',
          content: [{ type: 'text', text: 'Important', marks: [{ type: 'bold' }] }],
        },
      ],
    })

    expect(isPlainTextDocument(marked ?? [])).toBe(false)
  })

  it('makes a blank line a paragraph and a single newline a break', () => {
    expect(fromPlainText('One\ntwo\n\nThree')).toEqual({
      type: 'doc',
      content: [
        {
          type: 'paragraph',
          content: [
            { type: 'text', text: 'One' },
            { type: 'hardBreak' },
            { type: 'text', text: 'two' },
          ],
        },
        { type: 'paragraph', content: [{ type: 'text', text: 'Three' }] },
      ],
    })
  })

  it('refuses a link scheme that is not http, https or mailto', () => {
    expect(safeHref('https://example.com/spec')).toBe('https://example.com/spec')
    expect(safeHref('mailto:ada@example.com')).toBe('mailto:ada@example.com')

    /*
      The server already refuses these. Checked again because the value has
      been through a database, a cache and a JSON parse since then, and an
      `href` is the one attribute in this vocabulary that becomes executable
      if it is wrong.
    */
    expect(safeHref('javascript:alert(1)')).toBeNull()
    expect(safeHref('JaVaScRiPt:alert(1)')).toBeNull()
    expect(safeHref('data:text/html,<script>alert(1)</script>')).toBeNull()
    expect(safeHref(42)).toBeNull()
  })
})

describe('the documents screen is navigable without a mouse', () => {
  it('names every button, once', async () => {
    await openDocuments([
      documentDetail(),
      documentDetail({ id: '00000000-0000-4000-8000-0000000ff002', title: 'Retro' }),
    ])

    await screen.findByRole('complementary', { name: 'Document Launch plan' })

    expectEveryButtonNamed()
    expectNoDuplicateButtonNames()
  })

  it('has one first-level heading and an unbroken outline', async () => {
    await openDocuments([documentDetail({ content: FORMATTED_BODY })])

    await screen.findByText('Scope')

    /*
      The document's own headings are offset down by two: the page title is
      the `<h1>`, the document's title is the panel's `<h2>`, so a level-1
      heading inside the body renders as `<h3>`. Without the offset a
      document that starts with a title would put a second `<h1>` on the page.
    */
    expectOneFirstLevelHeading('Documents')
    expectHeadingLevelsUnbroken()
  })

  it('labels every control the composer draws', async () => {
    const app = await openDocuments([documentDetail()])

    await app.user.click(screen.getByRole('button', { name: 'New document' }))

    /*
      "Title", not "Document title" -- the open document's rename box owns
      that name and both are on the page at once. `findByRole` throwing on two
      matches is what caught the collision; the singular queries are the
      duplicate-label check, and no separate assertion is needed.
    */
    expect(await screen.findByRole('textbox', { name: 'Title' })).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Document title' })).toBeInTheDocument()
    expect(screen.getByRole('combobox', { name: 'Belongs to' })).toBeInTheDocument()
  })
})
