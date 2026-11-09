import { mkdirSync } from 'node:fs'

import { test as setup, expect } from '@playwright/test'

/**
 * Sign in once against staging and save the session, so the `staging` project
 * (which depends on this one) starts every spec already authenticated.
 *
 * Credentials come from e2e/.env, loaded by playwright.config.ts. baseURL is
 * the staging URL from the project's `use`.
 */
const STORAGE_STATE = 'e2e/.auth/user.json'

setup('authenticate against staging', async ({ page }) => {
  // Render free tier cold-starts; a sign-in can wait on a waking instance.
  setup.setTimeout(120_000)

  const email = process.env.E2E_EMAIL
  const password = process.env.E2E_PASSWORD
  if (!email || !password) {
    throw new Error('E2E_EMAIL / E2E_PASSWORD are not set (expected in e2e/.env)')
  }

  await page.goto('/login')
  await expect(page.getByRole('heading', { name: 'Sign in to Vector' })).toBeVisible({
    timeout: 90_000,
  })
  await page.getByLabel('Email', { exact: true }).fill(email)
  await page.getByLabel('Password', { exact: true }).fill(password)
  await page.getByRole('button', { name: 'Sign in' }).click()

  // Signing in sets the session cookie and the guard redirects off /login;
  // leaving /login is the signal the session is live.
  await page.waitForURL((url) => url.pathname !== '/login', { timeout: 90_000 })

  mkdirSync('e2e/.auth', { recursive: true })
  await page.context().storageState({ path: STORAGE_STATE })
})
