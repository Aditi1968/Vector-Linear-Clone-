import { test, expect } from '@playwright/test'

import { workspaceSlug } from '../workspace'

/**
 * The staging project's canary: proves the saved session authenticates and
 * this run's freshly provisioned workspace renders. If this fails, nothing
 * else here is worth reading.
 */
test('issues list renders for the signed-in workspace', async ({ page }) => {
  await page.goto(`/${workspaceSlug()}/issues`)
  await expect(page.getByRole('heading', { name: 'Issues' })).toBeVisible()
})
