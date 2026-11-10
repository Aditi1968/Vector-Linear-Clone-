import { test, expect } from '@playwright/test'
import type { Locator, Page } from '@playwright/test'

import { expectMutation } from '../helpers'
import { workspaceSlug } from '../workspace'

/**
 * Collaboration panels on the issue detail view against live staging (the
 * shared `e2e-playground` workspace, team key `QA`): Comments, Labels,
 * Relations and Sub-issues. Every test mints its own issue(s) named
 * `e2e <ISO timestamp>` and asserts on that record, never on counts or a
 * "first row".
 *
 * `settle()` + the composer helpers are copied verbatim from the
 * VERIFIED-GREEN issues.spec.ts: the inline composer loses its submit click if
 * the list's own queries are still in flight, so the network must go idle
 * before the click. This is a test-timing fix, not a product bug.
 */

const WS = `/${workspaceSlug()}`

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/** A row/card link's accessible name embeds the title; match it as a substring. */
function byTitle(title: string): RegExp {
  return new RegExp(escapeRegExp(title))
}

async function settle(page: Page): Promise<void> {
  await page.waitForLoadState('networkidle')
}

/**
 * Create an issue through the list composer and leave the browser on the list.
 * Asserts the IssueCreate round trip succeeded and the new row appeared.
 */
async function createIssue(page: Page, title: string): Promise<void> {
  await page.goto(`${WS}/issues`)
  await expect(page.getByRole('heading', { name: 'Issues' })).toBeVisible()
  await settle(page)

  await page.getByRole('button', { name: 'New issue' }).click()
  await expect(page.getByRole('heading', { name: 'New issue' })).toBeVisible()
  await settle(page)

  await page.getByRole('textbox', { name: 'Title' }).fill(title)

  const created = expectMutation(page, 'IssueCreate')
  await page.getByRole('button', { name: 'Create issue' }).click()
  await created

  await expect(page.getByRole('link', { name: byTitle(title) })).toBeVisible()
}

/** Create an issue, then open its inspector (the "Issue detail" side panel). */
async function createAndOpenIssue(page: Page, title: string): Promise<Locator> {
  await createIssue(page, title)
  await page.getByRole('link', { name: byTitle(title) }).click()
  const detail = page.getByRole('complementary', { name: 'Issue detail' })
  await expect(detail).toBeVisible()
  await settle(page)
  return detail
}

/**
 * Drive the IssuePicker (an IssueSearch query behind a dialog) to pick `title`.
 * The dialog is already open. Types the title, waits for the matching result
 * row to appear, and clicks it. IssueSearch is a query, so we assert the row
 * appears rather than wrapping it in expectMutation.
 */
async function pickInPicker(page: Page, dialogName: string, title: string): Promise<void> {
  const dialog = page.getByRole('dialog', { name: dialogName })
  await expect(dialog).toBeVisible()
  await dialog.getByRole('searchbox', { name: 'Search issues' }).fill(title)
  // The result button is named "{identifier} {title}"; match on the title.
  const result = dialog.getByRole('button', { name: byTitle(title) })
  await expect(result).toBeVisible()
  await result.click()
}

test.describe('/e2e-playground issue detail — collaboration', () => {
  test('CommentCreate + CommentDelete: post a comment then withdraw it', async ({ page }) => {
    const stamp = new Date().toISOString()
    const detail = await createAndOpenIssue(page, `e2e ${stamp}`)
    const comments = detail.getByRole('region', { name: 'Comments' })

    const body = `e2e comment ${stamp}`
    await comments.getByRole('textbox', { name: 'Add a comment' }).fill(body)

    const posted = expectMutation(page, 'CommentCreate')
    await comments.getByRole('button', { name: 'Comment', exact: true }).click()
    await posted

    await expect(comments.getByText(body)).toBeVisible()

    // Delete: "Delete comment by {me}" → confirm dialog "Delete comment".
    await comments.getByRole('button', { name: /^Delete comment by / }).click()
    const dialog = page.getByRole('dialog', { name: 'Delete this comment?' })
    const removed = expectMutation(page, 'CommentDelete')
    await dialog.getByRole('button', { name: 'Delete comment', exact: true }).click()
    await removed

    await expect(comments.getByText(body)).toHaveCount(0)
  })

  test('LabelCreate + IssueLabelAttach + IssueLabelDetach: create, attach, remove a label', async ({
    page,
  }) => {
    const stamp = new Date().toISOString()
    const detail = await createAndOpenIssue(page, `e2e ${stamp}`)
    const labels = detail.getByRole('region', { name: 'Labels' })

    const labelName = `e2e-label-${stamp}`

    await labels.getByRole('button', { name: 'Add', exact: true }).click()
    await page.getByRole('menuitem', { name: 'New label' }).click()
    await labels.getByRole('textbox', { name: 'New label name' }).fill(labelName)

    // "New label" is two mutations: LabelCreate, then IssueLabelAttach.
    const labelCreated = expectMutation(page, 'LabelCreate')
    const attached = expectMutation(page, 'IssueLabelAttach')
    await labels.getByRole('button', { name: 'Create', exact: true }).click()
    await labelCreated
    await attached

    // Chip appears, with its own "Remove {label}" button.
    const chipRemove = labels.getByRole('button', { name: `Remove ${labelName}` })
    await expect(chipRemove).toBeVisible()

    const detached = expectMutation(page, 'IssueLabelDetach')
    await chipRemove.click()
    await detached

    await expect(labels.getByRole('button', { name: `Remove ${labelName}` })).toHaveCount(0)
  })

  test('IssueRelationCreate + IssueRelationDelete: link two issues, then unlink', async ({
    page,
  }) => {
    const stamp = new Date().toISOString()
    // Issue B is the link target; create it first so it is searchable.
    const titleB = `e2e rel-target ${stamp}`
    await createIssue(page, titleB)

    const titleA = `e2e rel-source ${stamp}`
    const detail = await createAndOpenIssue(page, titleA)
    const relations = detail.getByRole('region', { name: 'Relations' })

    await relations.getByRole('button', { name: 'Add', exact: true }).click()
    // Default relation type is "Blocked by".
    const created = expectMutation(page, 'IssueRelationCreate')
    await pickInPicker(page, 'Link another issue', titleB)
    await created

    const relationLink = relations.getByRole('link', { name: byTitle(titleB) })
    await expect(relationLink).toBeVisible()

    // Remove button name = "Remove {group label lowercased} {title}".
    const removeRelation = relations.getByRole('button', {
      name: `Remove blocked by ${titleB}`,
    })
    const deleted = expectMutation(page, 'IssueRelationDelete')
    await removeRelation.click()
    await deleted

    await expect(relations.getByRole('link', { name: byTitle(titleB) })).toHaveCount(0)
  })

  test('IssueSetParent + IssueClearParent: add a sub-issue, then remove it', async ({ page }) => {
    const stamp = new Date().toISOString()
    // The child is the pick target; create it first so it is searchable.
    const childTitle = `e2e child ${stamp}`
    await createIssue(page, childTitle)

    const parentTitle = `e2e parent ${stamp}`
    const detail = await createAndOpenIssue(page, parentTitle)
    const subIssues = detail.getByRole('region', { name: 'Sub-issues' })

    await subIssues.getByRole('button', { name: 'Add', exact: true }).click()
    const set = expectMutation(page, 'IssueSetParent')
    await pickInPicker(page, 'Add a sub-issue', childTitle)
    await set

    const childLink = subIssues.getByRole('link', { name: byTitle(childTitle) })
    await expect(childLink).toBeVisible()

    const removeChild = subIssues.getByRole('button', {
      name: `Remove ${childTitle} as a sub-issue`,
    })
    const cleared = expectMutation(page, 'IssueClearParent')
    await removeChild.click()
    await cleared

    await expect(subIssues.getByRole('link', { name: byTitle(childTitle) })).toHaveCount(0)
  })

  // IssueComments fetchMore: skipped. Paginating the thread needs more comments
  // than the page size on a single issue; each test posts one comment, so
  // "Load more comments" never renders. Driving it would mean posting dozens of
  // comments to one issue on the small shared instance for one query — not worth
  // the run time. Inventory-only here.
})
