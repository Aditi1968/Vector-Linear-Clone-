import { test, expect } from '@playwright/test'
import type { Page } from '@playwright/test'

import { expectMutation } from '../helpers'

/**
 * Views group against live staging: saved views, favorites, documents, search,
 * semantic search, analytics. Each test mints its own data named `e2e <ISO>`
 * and asserts on that item. Only the e2e-playground workspace is touched.
 */

const WS = '/e2e-playground'

function unique(label: string): string {
  return `e2e ${label} ${new Date().toISOString()}`
}

/** Create a saved view by name on the saved-views screen, leaving it there. */
async function createSavedView(page: Page, name: string): Promise<void> {
  await page.goto(`${WS}/saved-views`)
  await page.getByRole('button', { name: 'New view' }).click()
  const dialog = page.getByRole('dialog', { name: 'New saved view' })
  await dialog.getByLabel('Name', { exact: true }).fill(name)
  const created = expectMutation(page, 'SavedViewCreate')
  await dialog.getByRole('button', { name: 'Create view' }).click()
  await created
  await expect(page.getByRole('button', { name: `Actions on ${name}` })).toBeVisible()
}

/** Delete a saved view by name on the saved-views screen. */
async function deleteSavedView(page: Page, name: string): Promise<void> {
  await page.goto(`${WS}/saved-views`)
  const actions = page.getByRole('button', { name: `Actions on ${name}` })
  if ((await actions.count()) === 0) {
    return
  }
  await actions.first().click()
  const deleted = expectMutation(page, 'SavedViewDelete')
  await page.getByRole('menuitem', { name: 'Delete view' }).click()
  await deleted
}

test.describe('saved views', () => {
  test('SavedViewCreate, SavedViewUpdate, SavedViewDelete', async ({ page }) => {
    const name = unique('view')
    const renamed = `${name} renamed`

    // Create
    await page.goto(`${WS}/saved-views`)
    await page.getByRole('button', { name: 'New view' }).click()
    const newDialog = page.getByRole('dialog', { name: 'New saved view' })
    await newDialog.getByLabel('Name', { exact: true }).fill(name)
    const created = expectMutation(page, 'SavedViewCreate')
    await newDialog.getByRole('button', { name: 'Create view' }).click()
    await created
    await expect(page.getByRole('button', { name: `Actions on ${name}` })).toBeVisible()

    // Update (rename via Edit view...)
    await page.getByRole('button', { name: `Actions on ${name}` }).click()
    await page.getByRole('menuitem', { name: 'Edit view...' }).click()
    const editDialog = page.getByRole('dialog', { name: 'Edit saved view' })
    await editDialog.getByLabel('Name', { exact: true }).fill(renamed)
    const updated = expectMutation(page, 'SavedViewUpdate')
    await editDialog.getByRole('button', { name: 'Save changes' }).click()
    await updated
    await expect(page.getByRole('button', { name: `Actions on ${renamed}` })).toBeVisible()

    // Delete
    await page.getByRole('button', { name: `Actions on ${renamed}` }).click()
    const deleted = expectMutation(page, 'SavedViewDelete')
    await page.getByRole('menuitem', { name: 'Delete view' }).click()
    await deleted
    await expect(
      page.getByRole('button', { name: `Actions on ${renamed}` }),
    ).toHaveCount(0)
  })
})

test.describe('favorites', () => {
  test('FavoriteAdd then FavoriteRemove (a saved view)', async ({ page }) => {
    const viewName = unique('fav-view')
    await createSavedView(page, viewName)

    await page.goto(`${WS}/favorites`)
    // The header Menu renders visible text "Add favorite"; the Menu component
    // only applies its `label` as aria-label when it has no children, so the
    // trigger's accessible name is the text, not the label.
    await page.getByRole('button', { name: 'Add favorite' }).click()
    const added = expectMutation(page, 'FavoriteAdd')
    await page.getByRole('menuitem', { name: viewName }).click()
    await added
    await expect(page.getByRole('button', { name: `Actions on ${viewName}` })).toBeVisible()

    await page.getByRole('button', { name: `Actions on ${viewName}` }).click()
    const removed = expectMutation(page, 'FavoriteRemove')
    await page.getByRole('menuitem', { name: 'Remove from favorites' }).click()
    await removed
    await expect(
      page.getByRole('button', { name: `Actions on ${viewName}` }),
    ).toHaveCount(0)

    await deleteSavedView(page, viewName)
  })

  test('FavoriteReorder (two of my own favorites)', async ({ page }) => {
    // Reorder renumbers the whole list, so if other favorites already exist it
    // would rewrite positions of data this test did not create. Only run when
    // the list starts empty; otherwise skip rather than touch foreign data.
    await page.goto(`${WS}/favorites`)
    const preexisting = await page.getByRole('button', { name: /^Actions on / }).count()
    test.skip(
      preexisting > 0,
      'favorites list is not empty; reorder renumbers all rows and would touch foreign data',
    )

    const first = unique('reorder-a')
    const second = unique('reorder-b')
    await createSavedView(page, first)
    await createSavedView(page, second)

    // Favorite both.
    for (const name of [first, second]) {
      await page.goto(`${WS}/favorites`)
      await page.getByRole('button', { name: 'Add favorite' }).click()
      const added = expectMutation(page, 'FavoriteAdd')
      await page.getByRole('menuitem', { name }).click()
      await added
      await expect(page.getByRole('button', { name: `Actions on ${name}` })).toBeVisible()
    }

    // Move the second one up: swaps it with the first (adjacent, both mine).
    const reordered = expectMutation(page, 'FavoriteReorder')
    await page.getByRole('button', { name: `Move ${second} up` }).click()
    await reordered

    // Clean up: remove both favorites, then delete both views.
    for (const name of [first, second]) {
      await page.goto(`${WS}/favorites`)
      const actions = page.getByRole('button', { name: `Actions on ${name}` })
      if ((await actions.count()) > 0) {
        await actions.first().click()
        const removed = expectMutation(page, 'FavoriteRemove')
        await page.getByRole('menuitem', { name: 'Remove from favorites' }).click()
        await removed
      }
    }
    await deleteSavedView(page, first)
    await deleteSavedView(page, second)
  })
})

test.describe('documents', () => {
  test('DocumentCreate, DocumentEdit (rename + body), DocumentCommentCreate, DocumentDelete', async ({
    page,
  }) => {
    const title = unique('doc')
    const renamed = `${title} renamed`
    const commentBody = `comment ${new Date().toISOString()}`

    // Create
    await page.goto(`${WS}/documents`)
    await page.getByRole('button', { name: 'New document' }).click()
    const dialog = page.getByRole('dialog', { name: 'New document' })
    await dialog.getByLabel('Title', { exact: true }).fill(title)
    const created = expectMutation(page, 'DocumentCreate')
    await dialog.getByRole('button', { name: 'Create document' }).click()
    await created
    await expect(page.getByRole('button', { name: `Actions on ${title}` })).toBeVisible()

    // Edit: rename
    await page.getByLabel('Document title', { exact: true }).fill(renamed)
    const renameSaved = expectMutation(page, 'DocumentEdit')
    // exact: the default substring match also catches buttons containing
    // "renamed" (this doc's own row/actions, and leftover docs).
    await page.getByRole('button', { name: 'Rename', exact: true }).click()
    await renameSaved
    await expect(page.getByRole('button', { name: `Actions on ${renamed}` })).toBeVisible()

    // Edit: body (a fresh document is empty plain text and editable here)
    const editBody = page.getByRole('button', { name: 'Edit body' })
    await expect(editBody).toBeVisible()
    await editBody.click()
    await page.getByLabel('Document body', { exact: true }).fill(`body ${title}`)
    const bodySaved = expectMutation(page, 'DocumentEdit')
    await page.getByRole('button', { name: 'Save body' }).click()
    await bodySaved

    // Comment
    await page.getByRole('tab', { name: 'Comments' }).click()
    await page.getByLabel('Add a comment', { exact: true }).fill(commentBody)
    const commented = expectMutation(page, 'DocumentCommentCreate')
    await page.getByRole('button', { name: 'Comment', exact: true }).click()
    await commented
    // The post-mutation refetch remounts the panel and resets the active tab
    // to "Document", so re-select Comments (now showing a count of 1) before
    // asserting the new comment is on screen.
    await expect(page.getByRole('tab', { name: 'Comments 1' })).toBeVisible()
    await page.getByRole('tab', { name: 'Comments 1' }).click()
    await expect(page.getByText(commentBody)).toBeVisible()

    // Delete
    await page.getByRole('button', { name: `Actions on ${renamed}` }).click()
    const deleted = expectMutation(page, 'DocumentDelete')
    await page.getByRole('menuitem', { name: 'Delete document' }).click()
    await deleted
    await expect(
      page.getByRole('button', { name: `Actions on ${renamed}` }),
    ).toHaveCount(0)
  })

  test('DocumentCommentCreate then DocumentCommentDelete', async ({ page }) => {
    test.setTimeout(90_000)
    const title = unique('doc-comment')
    // A token with no overlap with the title (the title contains the word
    // "comment"), so getByText(commentBody) matches only the comment.
    const commentBody = `e2e-cmt-${Math.random().toString(36).slice(2, 12)}`

    await page.goto(`${WS}/documents`)
    await page.getByRole('button', { name: 'New document' }).click()
    const dialog = page.getByRole('dialog', { name: 'New document' })
    await dialog.getByLabel('Title', { exact: true }).fill(title)
    const created = expectMutation(page, 'DocumentCreate')
    await dialog.getByRole('button', { name: 'Create document' }).click()
    await created

    // Give the document a body first. The proven comment-read path
    // (DocumentCommentCreate test above) runs against a document with a body;
    // a body-less document's comment-count tab updates unreliably under
    // concurrent load, so mirror the working path.
    await page.getByRole('button', { name: 'Edit body' }).click()
    await page.getByLabel('Document body', { exact: true }).fill(`body ${title}`)
    const bodySaved = expectMutation(page, 'DocumentEdit')
    await page.getByRole('button', { name: 'Save body' }).click()
    await bodySaved

    await page.getByRole('tab', { name: /^Comments/ }).click()
    await page.getByLabel('Add a comment', { exact: true }).fill(commentBody)
    const commented = expectMutation(page, 'DocumentCommentCreate')
    await page.getByRole('button', { name: 'Comment', exact: true }).click()
    await commented

    // Posting resets the active tab to "Document" and bumps the Comments count;
    // wait for that count (the refetch landed), then open it to see the comment.
    await expect(page.getByRole('tab', { name: 'Comments 1' })).toBeVisible()
    await page.getByRole('tab', { name: 'Comments 1' }).click()
    await expect(page.getByText(commentBody)).toBeVisible()

    // Delete the comment (DocumentCommentDelete). Its accessible name embeds the
    // revision stamp, so match on the stable prefix.
    const removed = expectMutation(page, 'DocumentCommentDelete')
    await page.getByRole('button', { name: /^Delete the comment/ }).click()
    await removed
    await expect(page.getByText(commentBody)).toHaveCount(0)

    // Clean up the document.
    await page.getByRole('button', { name: `Actions on ${title}` }).click()
    const deleted = expectMutation(page, 'DocumentDelete')
    await page.getByRole('menuitem', { name: 'Delete document' }).click()
    await deleted
  })

  test('DocumentRestore brings back a snapshotted version', async ({ page }) => {
    test.setTimeout(90_000)
    const title = unique('doc-restore')

    await page.goto(`${WS}/documents`)
    await page.getByRole('button', { name: 'New document' }).click()
    const dialog = page.getByRole('dialog', { name: 'New document' })
    await dialog.getByLabel('Title', { exact: true }).fill(title)
    const created = expectMutation(page, 'DocumentCreate')
    await dialog.getByRole('button', { name: 'Create document' }).click()
    await created

    // First body save establishes "version one" content.
    await page.getByRole('button', { name: 'Edit body' }).click()
    await page.getByLabel('Document body', { exact: true }).fill(`version one ${title}`)
    const firstSave = expectMutation(page, 'DocumentEdit')
    await page.getByRole('button', { name: 'Save body' }).click()
    await firstSave

    // Second save keeps the CURRENT ("version one") text as a version before
    // replacing it, so a real prior version exists to restore.
    await page.getByRole('button', { name: 'Edit body' }).click()
    await page.getByLabel('Document body', { exact: true }).fill(`version two ${title}`)
    await page.getByRole('checkbox', { name: /Keep the current text as a version/ }).check()
    const secondSave = expectMutation(page, 'DocumentEdit')
    await page.getByRole('button', { name: 'Save body' }).click()
    await secondSave

    // The History tab now has that version.
    await page.getByRole('tab', { name: /^History/ }).click()
    const restore = page.getByRole('button', { name: /^Restore the version/ })
    await expect(restore.first()).toBeVisible()
    const restored = expectMutation(page, 'DocumentRestore')
    await restore.first().click()
    await restored

    // Clean up.
    await page.getByRole('button', { name: `Actions on ${title}` }).click()
    const deleted = expectMutation(page, 'DocumentDelete')
    await page.getByRole('menuitem', { name: 'Delete document' }).click()
    await deleted
  })
})

test.describe('search', () => {
  test('WorkspaceSearch surfaces a freshly created issue', async ({ page }) => {
    const title = unique('search-issue')

    // Create an issue via the shell composer on the issues screen. The
    // composer files into teams[0] from the workspace-context query; wait for
    // a context-resolved row (its status is resolved from that same query)
    // before submitting, or a cold instance hits "no team to file issues in".
    await page.goto(`${WS}/issues`)
    await expect(page.getByRole('link', { name: /Status:/ }).first()).toBeVisible()
    await page.getByRole('button', { name: 'New issue' }).click()
    await page.getByLabel('Title', { exact: true }).fill(title)
    const created = expectMutation(page, 'IssueCreate')
    await page.getByRole('button', { name: 'Create issue' }).click()
    await created

    // Search for it (debounced input, no submit button).
    await page.goto(`${WS}/search`)
    await page.getByLabel('Search', { exact: true }).fill(title)
    await expect(page.getByRole('link', { name: title })).toBeVisible()
  })
})

test.describe('semantic search', () => {
  test('EmbeddingsRefresh then SemanticIssueMatches returns', async ({ page }) => {
    await page.goto(`${WS}/semantic-search`)
    // Let the index panel resolve before deciding whether indexing is offered.
    await expect(page.getByText(/Enabled|Not available/)).toBeVisible()

    const indexBtn = page.getByRole('button', { name: 'Index a batch now' })
    const enabled = await indexBtn.isVisible()
    test.skip(!enabled, 'embedding index is not enabled on this staging deployment')

    const refreshed = expectMutation(page, 'EmbeddingsRefresh')
    await indexBtn.click()
    await refreshed

    // Find similar: SemanticIssueMatches is a query -- assert the result UI
    // reaches a successful state (results or a successful empty answer), not
    // the "search failed" state.
    await page
      .getByLabel('Describe the issue', { exact: true })
      .fill('the build keeps failing halfway through CI')
    await page.getByRole('button', { name: 'Find similar issues' }).click()
    await expect(page.getByText(/near in meaning/)).toBeVisible()
  })
})

test.describe('analytics', () => {
  test('WorkspaceAnalytics re-queries when the date range changes', async ({ page }) => {
    await page.goto(`${WS}/analytics`)
    await expect(page.getByRole('heading', { name: 'Analytics' })).toBeVisible()
    // Figures from the default window are present before the change.
    await expect(page.getByText('Issues created')).toBeVisible()

    const requery = page.waitForResponse(
      (response) =>
        response.url().includes('/graphql') &&
        response.request().method() === 'POST' &&
        (() => {
          try {
            return (
              JSON.parse(response.request().postData() ?? '{}') as {
                operationName?: string
              }
            ).operationName === 'WorkspaceAnalytics'
          } catch {
            return false
          }
        })(),
    )
    await page.getByLabel('Date range', { exact: true }).selectOption('90')
    const response = await requery
    expect(response.status(), 'WorkspaceAnalytics: HTTP status').toBe(200)

    // Figures are still on screen after the re-query.
    await expect(page.getByText('Issues created')).toBeVisible()
  })
})
