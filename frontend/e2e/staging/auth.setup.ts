import { mkdirSync, writeFileSync } from 'node:fs'

import { test as setup, expect } from '@playwright/test'
import type { APIRequestContext } from '@playwright/test'

import { WORKSPACE_FILE } from '../workspace'

/**
 * Provision a FRESH, isolated workspace for this run, over the GraphQL API.
 *
 * Each run registers a throwaway user (`vector-e2e-<epoch>@example.com`),
 * creates its own workspace (`vector-e2e-<epoch>`) and a `QA` team, then saves
 * the session so every staging spec starts signed in to an EMPTY workspace of
 * its own. That is what makes the suite deterministic: no spec shares data or
 * competes for space in a crowded shared workspace.
 *
 * The API, not the UI: `register` sets the session cookie, so the two creates
 * that follow are authenticated, and `storageState()` exports that cookie for
 * the browser specs. It is faster and less flaky than driving onboarding, and
 * the onboarding UI itself is covered by the local `journeys` suite.
 */
const STORAGE_STATE = 'e2e/.auth/user.json'

const REGISTER = `mutation Register($input: RegisterInput!) {
  register(input: $input) { user { id email } errors { field code message } }
}`
const WORKSPACE_CREATE = `mutation WorkspaceCreate($input: WorkspaceCreateInput!) {
  workspaceCreate(input: $input) { workspace { slug } errors { field code message } }
}`
const TEAM_CREATE = `mutation TeamCreate($input: TeamCreateInput!) {
  teamCreate(input: $input) { team { key } errors { field code message } }
}`

setup('provision a fresh isolated workspace', async ({ playwright }) => {
  // Render free tier cold-starts; the first request of a run can wait.
  setup.setTimeout(120_000)

  const baseURL = process.env.E2E_BASE_URL
  const password = process.env.E2E_PASSWORD
  if (!baseURL) throw new Error('E2E_BASE_URL is not set (expected in e2e/.env or CI secrets)')
  if (!password) throw new Error('E2E_PASSWORD is not set (expected in e2e/.env or CI secrets)')

  const stamp = Date.now()
  const slug = `vector-e2e-${stamp}` // slug rule: lowercase letters/digits/hyphens
  const email = `vector-e2e-${stamp}@example.com`

  const api = await playwright.request.newContext({ baseURL })
  const gql = async (
    query: string,
    variables: Record<string, unknown>,
    label: string,
  ): Promise<Record<string, any>> => {
    const response = await api.post('/graphql', { data: { query, variables } })
    expect(response.status(), `${label}: HTTP status`).toBe(200)
    const body = (await response.json()) as { data?: Record<string, any>; errors?: unknown }
    expect(body.errors ?? null, `${label}: GraphQL transport errors`).toBeNull()
    return body.data as Record<string, any>
  }

  const reg = await gql(REGISTER, { input: { email, password, name: 'E2E Run' } }, 'register')
  expect(reg.register.errors, 'register validation errors').toEqual([])
  expect(reg.register.user, 'register returned a user').not.toBeNull()

  const ws = await gql(WORKSPACE_CREATE, { input: { name: slug, slug } }, 'workspaceCreate')
  expect(ws.workspaceCreate.errors, 'workspaceCreate validation errors').toEqual([])

  const team = await gql(
    TEAM_CREATE,
    { input: { workspaceSlug: slug, name: 'QA', key: 'QA' } },
    'teamCreate',
  )
  expect(team.teamCreate.errors, 'teamCreate validation errors').toEqual([])

  mkdirSync('e2e/.auth', { recursive: true })
  await api.storageState({ path: STORAGE_STATE })
  writeFileSync(WORKSPACE_FILE, `${JSON.stringify({ slug, email }, null, 2)}\n`)
  await (api as APIRequestContext).dispose()
})
