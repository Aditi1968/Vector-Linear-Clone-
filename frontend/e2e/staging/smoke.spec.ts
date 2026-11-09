import { test, expect } from '@playwright/test'

/**
 * The staging project's canary: proves the saved session authenticates and
 * the e2e-playground workspace renders. If this fails, nothing else here is
 * worth reading.
 */
test('issues list renders for the signed-in workspace', async ({ page }) => {
  await page.goto('/e2e-playground/issues')
  await expect(page.getByRole('heading', { name: 'Issues' })).toBeVisible()
})
