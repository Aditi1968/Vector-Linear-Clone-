import { test, expect } from '@playwright/test'
import type { Page } from '@playwright/test'

import { expectMutation } from '../helpers'

/**
 * Shell chrome against live staging (the shared `e2e-playground` workspace,
 * team key `QA`): the command palette, the sidebar nav, and the account menu's
 * logout. Mounted by `AppLayout` on every workspace route.
 *
 * Two operations are observable mutations/queries and get `expectMutation`:
 * the palette's **CommandSearch** (typing a query) and **Logout**. The nav
 * links and the palette's "Go to" entries are client navigation only (NAV),
 * so they are asserted by URL + `aria-current`, not by a GraphQL round trip.
 *
 * Logout is isolated per rule 3: it deletes the session row keyed by the
 * shared cookie, so signing out of the shared `storageState` would sign out
 * every other parallel test. That one test opens its own fresh context and
 * logs in as a second session before logging out.
 */

const WS = '/e2e-playground'

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/** A row/option's accessible name embeds the title; match it as a substring. */
function byTitle(title: string): RegExp {
  return new RegExp(escapeRegExp(title))
}

/**
 * A unique all-letter token the Postgres full-text index matches as one
 * lexeme. `CommandSearch` runs `websearch_to_tsquery('english', ...)`, so the
 * query and the title stem through the same config and the match is exact; a
 * random word makes the result set for this test exactly one issue -- mine.
 */
function searchToken(): string {
  return Array.from({ length: 10 }, () =>
    String.fromCharCode(97 + Math.floor(Math.random() * 26)),
  ).join('')
}

async function settle(page: Page): Promise<void> {
  await page.waitForLoadState('networkidle')
}

/**
 * Create an issue through the list composer and leave the browser on the list.
 * The same settle() dance issues.spec.ts documents: the inline composer drops
 * a submit clicked while the list's queries are still in flight.
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

test.describe('/e2e-playground shell — command palette', () => {
  test('CommandSearch: a search result row navigates to its issue', async ({ page }) => {
    const token = searchToken()
    const title = `e2e shell ${token} ${new Date().toISOString()}`
    await createIssue(page, title)

    // Open the palette from the rail's "Command" button.
    await page.getByRole('button', { name: 'Command' }).click()
    const palette = page.getByRole('dialog', { name: 'Command palette' })
    await expect(palette).toBeVisible()

    const input = palette.getByRole('combobox', {
      name: 'Search issues and projects, or run a command',
    })
    // Typing the token drives CommandSearch; the unique token returns my issue.
    const searched = expectMutation(page, 'CommandSearch')
    await input.fill(token)
    await searched

    // CommandSearch drives the result rows: my issue is a listbox option.
    const result = palette.getByRole('option', { name: byTitle(title) })
    await expect(result).toBeVisible()

    // Clicking the row navigates to that issue's detail view.
    await result.click()
    await expect(page).toHaveURL(/\/e2e-playground\/issues\/.+/)
    await expect(
      page.getByRole('complementary', { name: 'Issue detail' }),
    ).toBeVisible()
  })

  test('Command palette: a "Go to" entry navigates', async ({ page }) => {
    await page.goto(`${WS}/issues`)
    await settle(page)

    // Open via the Ctrl+K chord (the other documented way in).
    await page.keyboard.press('Control+K')
    const palette = page.getByRole('dialog', { name: 'Command palette' })
    await expect(palette).toBeVisible()

    // Empty query: every "Go to" entry is listed and no CommandSearch fires.
    const goTo = palette.getByRole('option', { name: 'Projects', exact: true })
    await expect(goTo).toBeVisible()
    await goTo.click()

    await expect(page).toHaveURL(/\/e2e-playground\/projects$/)
    await expect(page.getByRole('heading', { name: 'Projects' })).toBeVisible()
  })
})

test.describe('/e2e-playground shell — navigation (NAV, no mutation)', () => {
  test('Nav links change the URL and mark the active row aria-current', async ({ page }) => {
    await page.goto(`${WS}/issues`)
    await settle(page)

    const links: ReadonlyArray<[string, RegExp]> = [
      ['Projects', /\/e2e-playground\/projects$/],
      ['Members', /\/e2e-playground\/members$/],
      ['All Issues', /\/e2e-playground\/issues$/],
    ]

    for (const [name, url] of links) {
      const link = page.getByRole('link', { name, exact: true })
      await link.click()
      await expect(page).toHaveURL(url)
      await expect(link).toHaveAttribute('aria-current', 'page')
    }
  })
})

test.describe('/e2e-playground shell — logout', () => {
  test('Logout signs out (isolated session)', async ({ browser }) => {
    // A fresh context, NOT the shared storageState: logging out here deletes
    // only this second session's row (rule 3).
    const context = await browser.newContext({ storageState: undefined })
    const page = await context.newPage()
    try {
      await page.goto('/login') // baseURL is set in playwright.config.ts
      await page
        .getByRole('heading', { name: 'Sign in to Vector' })
        .waitFor({ timeout: 60_000 })
      await page.getByLabel('Email', { exact: true }).fill(process.env.E2E_EMAIL!)
      await page.getByLabel('Password', { exact: true }).fill(process.env.E2E_PASSWORD!)
      await page.getByRole('button', { name: 'Sign in' }).click()
      // Generous for a cold staging start.
      await page.waitForURL((u) => u.pathname !== '/login', { timeout: 60_000 })

      await page.getByRole('button', { name: 'Account' }).click()
      const out = expectMutation(page, 'Logout')
      await page.getByRole('menuitem', { name: 'Sign out' }).click()
      await out

      // Redirected to the public landing page.
      await expect(page).toHaveURL(/\/$|landing/)
    } finally {
      await context.close()
    }
  })
})
