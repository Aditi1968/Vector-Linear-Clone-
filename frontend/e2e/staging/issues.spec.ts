import { test, expect } from '@playwright/test'
import type { Page } from '@playwright/test'

import { expectMutation } from '../helpers'

/**
 * Issues cluster against live staging (the shared `e2e-playground` workspace,
 * team key `QA`). Every test mints its own data named `e2e <ISO timestamp>`
 * and asserts on that record, never on counts or "first row".
 *
 * One staging-specific reliability note drives the `settle()` calls below: the
 * inline issue composer submits nothing if its button is clicked while the
 * list's own queries are still in flight -- the list resolving re-renders the
 * tree and the click is lost. Waiting for the network to go idle after the
 * composer opens makes the submit deterministic. This is a test-timing fix,
 * not a product bug: once the page has settled, IssueCreate fires every time.
 */

const WS = '/e2e-playground'

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/** A row/card link's accessible name embeds the title; match it as a substring. */
function byTitle(title: string): RegExp {
  return new RegExp(escapeRegExp(title))
}

/**
 * A unique 2-week window far in the future, as datetime-local strings.
 *
 * A team's cycles may not overlap (`CycleCreate` rejects an overlap with
 * `OVERLAPPING`), and the shared workspace accumulates every test's cycles, so
 * a fixed date would collide on the second run. A random day across a
 * millennium makes a clash between runs vanishingly unlikely.
 */
function futureCycleWindow(): { starts: string; ends: string } {
  const base = Date.UTC(2200, 0, 1)
  const start = new Date(base + Math.floor(Math.random() * 400_000) * 86_400_000)
  const end = new Date(start.getTime() + 13 * 86_400_000)
  const fmt = (d: Date) => d.toISOString().slice(0, 16) // YYYY-MM-DDTHH:MM
  return { starts: fmt(start), ends: fmt(end) }
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
async function createAndOpenIssue(page: Page, title: string) {
  await createIssue(page, title)
  await page.getByRole('link', { name: byTitle(title) }).click()
  const detail = page.getByRole('complementary', { name: 'Issue detail' })
  await expect(detail).toBeVisible()
  await settle(page)
  return detail
}

test.describe('/e2e-playground/issues — list + composer', () => {
  test('IssueCreate: New issue composer files an issue', async ({ page }) => {
    const stamp = new Date().toISOString()
    const title = `e2e ${stamp}`
    await createIssue(page, title)
    // Row link carries the new title (createIssue already asserts visibility).
    await expect(page.getByRole('link', { name: byTitle(title) })).toBeVisible()
  })

  test('IssueList: Load more fetches the next page', async ({ page }) => {
    await page.goto(`${WS}/issues`)
    await expect(page.getByRole('heading', { name: 'Issues' })).toBeVisible()
    await settle(page)

    const loadMore = page.getByRole('button', { name: /Load more/i })
    if (!(await loadMore.isVisible().catch(() => false))) {
      test.skip(
        true,
        'Issue list fits in a single page on staging ("End of list" shown); Load more is not rendered, so fetchMore has nothing to drive.',
      )
      return
    }
    const more = expectMutation(page, 'IssueList')
    await loadMore.click()
    await more
  })
})

test.describe('/e2e-playground/issues — inspector', () => {
  test('IssueUpdate: Status combobox changes the state', async ({ page }) => {
    const stamp = new Date().toISOString()
    const detail = await createAndOpenIssue(page, `e2e ${stamp}`)

    const updated = expectMutation(page, 'IssueUpdate')
    await detail.getByRole('combobox', { name: 'Status' }).selectOption({ label: 'In Progress' })
    await updated

    await expect(detail.getByRole('combobox', { name: 'Status' })).toHaveValue(/.+/)
    await expect(detail.getByRole('img', { name: 'Status: In Progress' })).toBeVisible()
  })

  test('IssueUpdate: Title EditableText saves on Enter', async ({ page }) => {
    const stamp = new Date().toISOString()
    const detail = await createAndOpenIssue(page, `e2e ${stamp}`)

    const newTitle = `e2e ${stamp} renamed`
    const title = detail.getByRole('textbox', { name: 'Title' })
    await title.fill(newTitle)

    const updated = expectMutation(page, 'IssueUpdate')
    await title.press('Enter')
    await updated

    await expect(detail.getByRole('textbox', { name: 'Title' })).toHaveValue(newTitle)
  })

  test('IssueSetProject: assign a freshly created project', async ({ page }) => {
    const stamp = new Date().toISOString()
    const projectName = `e2e project ${stamp}`

    // Create the project to assign (none exist in the shared workspace).
    await page.goto(`${WS}/projects`)
    await expect(page.getByRole('heading', { name: 'Projects' })).toBeVisible()
    await settle(page)
    await page.getByRole('button', { name: 'New project' }).click()
    const projectDialog = page.getByRole('dialog', { name: 'New project' })
    await projectDialog.getByRole('textbox', { name: 'Name' }).fill(projectName)
    await expect(projectDialog.getByRole('button', { name: 'Create project' })).toBeEnabled()
    const projectCreated = expectMutation(page, 'ProjectCreate')
    await projectDialog.getByRole('button', { name: 'Create project' }).click()
    await projectCreated

    const detail = await createAndOpenIssue(page, `e2e ${stamp}`)
    const project = detail.getByRole('combobox', { name: 'Project' })
    await expect(project.getByRole('option', { name: projectName })).toBeAttached()

    const set = expectMutation(page, 'IssueSetProject')
    await project.selectOption({ label: projectName })
    await set
  })

  test('IssueSetCycle: assign a freshly created cycle', async ({ page }) => {
    const stamp = new Date().toISOString()
    const cycleName = `e2e cycle ${stamp}`

    // Create a QA-team cycle to assign (none exist in the shared workspace).
    await page.goto(`${WS}/cycles`)
    await expect(page.getByRole('heading', { name: 'Cycles' })).toBeVisible()
    await settle(page)
    await page.getByRole('button', { name: 'New cycle' }).click()
    const cycleDialog = page.getByRole('dialog', { name: 'New cycle' })
    await cycleDialog.getByRole('textbox', { name: 'Name (optional)' }).fill(cycleName)
    // Starts/Ends are datetime-local inputs; a unique far-future window per
    // run avoids the team's no-overlap rule.
    const { starts, ends } = futureCycleWindow()
    await cycleDialog.getByRole('textbox', { name: 'Starts' }).fill(starts)
    await cycleDialog.getByRole('textbox', { name: 'Ends' }).fill(ends)
    await expect(cycleDialog.getByRole('button', { name: 'Create cycle' })).toBeEnabled()
    const cycleCreated = expectMutation(page, 'CycleCreate')
    await cycleDialog.getByRole('button', { name: 'Create cycle' }).click()
    await cycleCreated

    let detail = await createAndOpenIssue(page, `e2e ${stamp}`)
    // The inspector's Cycle list is served from cache and does not include a
    // cycle created moments earlier until something forces a refetch; a reload
    // runs the team-cycles query fresh.
    await page.reload()
    detail = page.getByRole('complementary', { name: 'Issue detail' })
    await expect(detail).toBeVisible()
    await settle(page)

    const cycle = detail.getByRole('combobox', { name: 'Cycle' })
    await expect(cycle.getByRole('option', { name: cycleName })).toBeAttached()

    const set = expectMutation(page, 'IssueSetCycle')
    await cycle.selectOption({ label: cycleName })
    await set
  })

  test('IssueArchive: More actions → Archive removes the row', async ({ page }) => {
    const stamp = new Date().toISOString()
    const title = `e2e ${stamp}`
    const detail = await createAndOpenIssue(page, title)

    await detail.getByRole('button', { name: /^More actions/ }).click()
    const archived = expectMutation(page, 'IssueArchive')
    await page.getByRole('menuitem', { name: 'Archive issue' }).click()
    await archived

    await expect(page.getByRole('link', { name: byTitle(title) })).toHaveCount(0)
  })
})

/**
 * Put the board in "Newest" sort so a just-created issue sits at the top of
 * its column and is on the first (unpaginated) page. The shared workspace has
 * enough issues that a Todo column paginates under the default Priority sort,
 * and a brand-new No-priority card can land past the first page. Sorting by
 * newest is itself a BoardIssues refetch.
 */
async function openBoardNewestFirst(page: Page): Promise<void> {
  await page.goto(`${WS}/board`)
  await expect(page.getByRole('heading', { name: 'Board' })).toBeVisible()
  await settle(page)
  const sorted = expectMutation(page, 'BoardIssues')
  await page.getByRole('combobox', { name: 'Sort by' }).selectOption({ label: 'Newest' })
  await sorted
}

test.describe('/e2e-playground/board', () => {
  test('BoardIssues: Status filter changes the result set', async ({ page }) => {
    const stamp = new Date().toISOString()
    const title = `e2e ${stamp}`
    await createIssue(page, title) // defaults to Todo

    await openBoardNewestFirst(page)

    // My brand-new Todo issue is at the top of the Todo column.
    await expect(page.getByRole('link', { name: byTitle(title) })).toBeVisible()

    // Filter to a status it is NOT in; the query re-runs and my card drops out.
    const filtered = expectMutation(page, 'BoardIssues')
    await page.getByRole('combobox', { name: 'Status' }).selectOption({ label: 'Done' })
    await filtered

    await expect(page.getByRole('link', { name: byTitle(title) })).toHaveCount(0)
  })

  test('BoardIssueMove: Move menu lands the card in a new column', async ({ page }) => {
    const stamp = new Date().toISOString()
    const title = `e2e ${stamp}`
    await createIssue(page, title) // Todo

    await openBoardNewestFirst(page)

    const card = page
      .getByRole('listitem')
      .filter({ has: page.getByRole('link', { name: byTitle(title) }) })
    await expect(card).toHaveCount(1)

    await card.getByRole('button', { name: /^Move / }).click()
    const moved = expectMutation(page, 'BoardIssueMove')
    await page.getByRole('menuitem', { name: 'In Progress' }).click()
    await moved

    await expect(
      page.getByRole('region', { name: 'In Progress' }).getByRole('link', { name: byTitle(title) }),
    ).toBeVisible()
  })
})

test.describe('/e2e-playground/triage', () => {
  test('Triage queue mutations (Accept/Decline/IssueUpdate)', async () => {
    test.skip(
      true,
      'No product UI files an issue without a state: the composer always assigns Todo, so an issue cannot be driven into the triage queue from the app ("Issues filed without a state land here" — that intake is API/email only). The queue stays empty on this single-member staging workspace, so TriageAccept, TriageDecline and TriageIssueUpdate are inventory-only here.',
    )
  })
})

test.describe('/e2e-playground/inbox', () => {
  test('NotificationInbox: Unread filter radio switches the query', async ({ page }) => {
    await page.goto(`${WS}/inbox`)
    await expect(page.getByRole('heading', { name: 'Inbox' })).toBeVisible()
    await settle(page)

    // The radio input is visually hidden; its label text is the click target.
    const switched = expectMutation(page, 'NotificationInbox')
    await page.getByText('Unread', { exact: true }).click()
    await switched

    await expect(page.getByRole('radio', { name: 'Unread' })).toBeChecked()
  })

  test('NotificationMarkAllRead: mark all notifications read', async ({ page }) => {
    await page.goto(`${WS}/inbox`)
    await expect(page.getByRole('heading', { name: 'Inbox' })).toBeVisible()
    await settle(page)

    const markAll = page.getByRole('button', { name: 'Mark all read' })
    if (await markAll.isDisabled()) {
      test.skip(
        true,
        'Single-member staging workspace has 0 unread notifications, so "Mark all read" is disabled (DISABLED-ONLY). Nothing to mark.',
      )
      return
    }
    const done = expectMutation(page, 'NotificationMarkAllRead')
    await markAll.click()
    await done
    await expect(markAll).toBeDisabled()
  })
})
